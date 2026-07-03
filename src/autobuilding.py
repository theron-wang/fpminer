from minisweagent.models.litellm_model import LitellmModel
from minisweagent.environments.docker import DockerEnvironment
from analysis_agent.mini_orchestrator import run_with_attempts

def enable_checkers(target_name: str, target_url: str, tool_name: str, tool_url: str):
    model = LitellmModel(model_name="gpt-5")
    env = DockerEnvironment(image="ubuntu:22.04")

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