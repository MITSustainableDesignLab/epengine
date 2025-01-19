"""Simulate an EnergyPlus model with associated artifacts."""

import logging
import re
import shutil
import tempfile
import time
from pathlib import Path

import pandas as pd
from archetypal import IDF
from archetypal.idfclass.sql import Sql
from hatchet_sdk.context import Context

from epengine.hatchet import hatchet
from epengine.models.ddy_injector import DDYSizingSpec
from epengine.models.leafs import SimulationSpec
from epengine.models.mixins import WithHContext
from epengine.utils.results import postprocess, serialize_df_dict

logger = logging.getLogger(__name__)


class SimulationSpecWithContext(WithHContext, SimulationSpec):
    """A simulation specification with a Hatchet Context."""

    pass


# TODO: This could be generated by a class method in the SimulationSpec class
# but should it?
@hatchet.workflow(
    name="simulate_epw_idf",
    on_events=["simulation:run_artifacts"],
    timeout="20m",
    version="0.3",
    schedule_timeout="1000m",
)
class Simulate:
    """A workflow to simulate an EnergyPlus model."""

    @hatchet.step(name="simulate", timeout="20m", retries=2)
    def simulate(self, context: Context):
        """Simulate an EnergyPlus model.

        Args:
            context (Context): The context of the workflow

        Returns:
            dict: A dictionary of dataframes with results.
        """
        data = context.workflow_input()
        data["hcontext"] = context
        spec = SimulationSpecWithContext(**data)
        with tempfile.TemporaryDirectory() as tmpdir:
            local_pth = Path(tmpdir) / "model.idf"
            shutil.copy(spec.idf_path, local_pth)
            idf = IDF(
                local_pth,  # pyright: ignore [reportArgumentType]
                epw=spec.epw_path,
                output_directory=tmpdir,
            )  # pyright: ignore [reportArgumentType]

            if spec.ddy_path:
                # add_sizing_design_day(idf, spec.ddy_path)
                ddy = IDF(
                    spec.ddy_path,  # pyright: ignore [reportArgumentType]
                    epw=spec.epw_path,
                    as_version="9.5.0",
                    file_version="9.5.0",
                    prep_outputs=False,
                )

                ddy_spec = DDYSizingSpec(
                    design_days=[
                        "Ann Clg .4% Condns DB=>MWB",
                        "Ann Htg 99.6% Condns DB",
                    ]
                )
                ddy_spec.inject_ddy(idf, ddy)

            spec.log(f"Simulating {spec.idf_path}...")

            try:
                idf.simulate()

            except Exception as e:
                spec.log(f"Error simulating {spec.idf_path}:\n{e}")
                # TODO: Use a flag to determine if we should actually store this data,
                # as when we have a very large run, the failure logs can get very large
                dfs = {}
                index_data = _generate_index_data(spec)
                err_index = pd.MultiIndex.from_tuples(
                    [tuple(index_data.values())],
                    names=list(index_data.keys()),
                )

                err_df = pd.DataFrame({"msg": [str(e)]}, index=err_index)
                dfs["err"] = err_df

            else:
                time.sleep(1)
                sql = Sql(idf.sql_file)
                index_data = _generate_index_data(spec)
                err_df = _generate_error_warning_counts_df(idf, index_data)
                dfs = postprocess(
                    sql,
                    index_data=index_data,
                    tabular_lookups=[
                        ("AnnualBuildingUtilityPerformanceSummary", "End Uses")
                    ],
                    columns=["Electricity", "Natural Gas", "Fuel Oil No 2"],
                )
                dfs["energyplus_message_counts"] = err_df

        dfs = serialize_df_dict(dfs)

        return dfs


def _generate_index_data(spec: SimulationSpecWithContext):
    """Generate index data from a spec for use in error and results DataFrames.

    Args:
        spec (BaseSpec): The spec to generate index data from. Must have hcontext.

    Returns:
        index_data (dict): The index data with workflow_run_id added
    """
    index_data = spec.model_dump(mode="json", exclude_none=True)
    workflow_run_id = spec.hcontext.workflow_run_id()
    index_data["workflow_run_id"] = workflow_run_id
    return index_data


def _generate_error_warning_counts_df(idf: IDF, index_data: dict) -> pd.DataFrame:
    """Generate error and warning counts DataFrame from IDF end file and index data.

    Args:
        idf (IDF): The IDF object that has been simulated
        index_data (dict): The index data to use for the DataFrame index

    Returns:
        err_df (pd.DataFrame): DataFrame containing warning and severe error counts
    """
    end_file = idf.simulation_dir / "eplusout.end"
    err_str = end_file.read_text()
    severe_reg = r".*\s(\d+)\sSevere Errors.*"
    warning_reg = r".*\s(\d+)\sWarning.*"
    severe_matcher = re.match(severe_reg, err_str)
    warning_matcher = re.match(warning_reg, err_str)
    severe_ct = int(severe_matcher.groups()[0]) if severe_matcher else 0
    warning_ct = int(warning_matcher.groups()[0]) if warning_matcher else 0

    err_index = pd.MultiIndex.from_tuples(
        [tuple(index_data.values())],
        names=list(index_data.keys()),
    )
    return pd.DataFrame(
        {"warnings": [warning_ct], "severe": [severe_ct]}, index=err_index
    )


def add_sizing_design_day(idf: IDF, ddy_file: Path | str):
    """Read ddy file and copy objects over to self.

    Note:
        Will **NOT** add the Rain file to the model
    """
    ddy = IDF(
        ddy_file,  # pyright: ignore [reportArgumentType]
        as_version="9.2.0",
        file_version="9.2.0",
        prep_outputs=False,
    )
    for objtype, sequence in ddy.idfobjects.items():
        if sequence:
            for obj in sequence:
                if obj.key.upper() in [
                    "SITE:PRECIPITATION",
                    "ROOFIRRIGATION",
                    "SCHEDULE:FILE",
                ] and getattr(obj, "File_Name", "rain").endswith("rain"):
                    continue

                idf.removeallidfobjects(objtype)
                # idf.removeallidfobjects()
                idf.addidfobject(obj)

    del ddy
