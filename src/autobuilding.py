import os

from minisweagent.models.litellm_model import LitellmModel
from minisweagent.environments.local import LocalEnvironment
from analysis_agent.mini_orchestrator import run_with_attempts

def enable_checkers(target_name: str, target_url: str, tool_name: str, tool_url: str):
    cwd = f"targets/{target_name}"

    if os.path.exists(cwd):
        return None

    model = LitellmModel(model_name="gpt-5")
    env = LocalEnvironment(
        cwd=cwd,
        timeout=60,
    )

    success, message = run_with_attempts(
        model=model,
        env=env,
        tool_name=tool_name,
        tool_url=tool_url,
        target_name=target_name,
        target_url=target_url,
        max_attempts=3,
        cycle_budget=40,
        mode="auto",
        time_limit_seconds=10800,
        enable_exit_attempt=True,
        exit_attempt_model="gpt-5.2",
    )

    return success, message