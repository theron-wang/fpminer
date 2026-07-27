import os
import shutil

from checker_setup.analysisagent import run_analysis_agent
from checker_setup.dljc import run_dljc

CF_URL = "https://github.com/typetools/checker-framework/"


def setup_checker(target_name: str, target_url: str, tool_name: str) -> str | None:
    """
    Sets up the given checker on the given target. Runs DLJC first; if that fails, then
    try AnalysisAgent. Returns the command to run the checker, or None if all failed.

    :param target_name: The target name
    :param target_url: The url to the target's repository
    :param tool_name: The tool name
    :return: The command to run the checker, or None if all failed
    """
    print(f"Setting up checker {tool_name} for target {target_name}")

    already_existed = os.path.exists(f"targets/{target_name}")

    command = run_dljc(target_name, target_url, tool_name)

    if command:
        print(f"DLJC succeeded for target {target_name} with tool {tool_name}")
        return command

    # dljc.py automatically clones the project. If the repository was already cloned before
    # the dljc run, and dljc failed, that means that it is the artifact of a previous
    # AnalysisAgent run and all we need to do is run the replay.
    if not already_existed:
        shutil.rmtree(f"targets/{target_name}")

    print(f"DLJC failed for target {target_name} with tool {tool_name}, trying AnalysisAgent")

    success, command = run_analysis_agent(target_name, target_url, tool_name, CF_URL)

    if success:
        print(f"AnalysisAgent succeeded for target {target_name} with tool {tool_name}")
        return command

    print(f"AnalysisAgent failed for target {target_name} with tool {tool_name}")
    return None
