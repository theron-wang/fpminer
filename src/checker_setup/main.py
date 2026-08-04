import os
import shutil

from checker_setup.analysisagent import run_analysis_agent
from checker_setup.dljc import run_dljc
from utils import CheckerError

CF_URL = "https://github.com/typetools/checker-framework/"


def setup_checker(target_name: str, target_url: str, tool_name: str) -> list[CheckerError] | None:
    """
    Sets up the given checker on the given target. Runs DLJC first; if that fails, then
    try AnalysisAgent. Returns the errors generated, or None if all failed. Note that
    this may return an empty list if the checker was set up successfully, but found
    no errors in the project.

    :param target_name: The target name
    :param target_url: The url to the target's repository
    :param tool_name: The tool name
    :return: The errors found by the checker, or None if all failed
    """

    already_existed = os.path.exists(f"targets/{target_name}")

    errors = run_dljc(target_name, target_url, tool_name)

    # dljc may run and give an empty list; in that case, it set up successfully but the
    # checker just didn't find any errors
    if errors is not None:
        return errors

    # dljc.py automatically clones the project. If the repository was already cloned before
    # the dljc run, and dljc failed, that means that it is the artifact of a previous
    # AnalysisAgent run and all we need to do is run the replay.
    if not already_existed:
        shutil.rmtree(f"targets/{target_name}")

    return run_analysis_agent(target_name, target_url, tool_name, CF_URL)
