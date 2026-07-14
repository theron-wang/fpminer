import io
import os
import re
import subprocess
import tarfile
from pathlib import Path
from typing import IO, cast

import tree_sitter_bash
from analysis_agent.mini_orchestrator import sanitize_for_filename
from analysis_agent.replay_producer import produce_replay
from tree_sitter import Language, Parser

# Available models:
# gemini/gemini-3-flash-preview
# gemini/gemini-3.5-flash
# gemini/gemini-2.5-flash
# gemini/gemini-2.5-flash-lite
# gemini/gemini-3.1-flash-lite
model_name = "gemini/gemini-3.1-flash-lite"


def enable_checkers(target_name: str, target_url: str, tool_name: str, tool_url: str):
    # model = LitellmModel(model_name=model_name)
    # env = DockerEnvironment(
    #     image="ubuntu:22.04"
    # )
    #
    # logging.basicConfig(
    #     level=logging.INFO,
    #     format='%(asctime)s - %(levelname)s - %(message)s'
    # )

    # TODO: enable dljc

    success = True

    print(f"Running AnalysisAgent on {target_name} with tool {tool_name}")

    if os.path.exists(f"targets/{target_name}"):
        print(f"Target {target_name} already exists, skipping")
    else:
        pass
        # success, message = run_with_attempts(
        #     model=model,
        #     env=env,
        #     tool_name=tool_name,
        #     tool_url=tool_url,
        #     target_name=target_name,
        #     target_url=target_url,
        #     max_attempts=3,
        #     cycle_budget=40,
        #     mode="auto",
        #     time_limit_seconds=10800,
        #     enable_exit_attempt=False
        # )

    if not success:
        return False, None

    # AnalysisAgent automatically cleans up the Docker container after execution,
    # so we need to reconstruct the output
    print(f"Reconstructing output for target {target_name} with tool {tool_name}")
    command = _reconstruct(target_name, tool_name, f"targets/{target_name}")
    print("Reconstruction complete.")
    print()

    return True, command


def _reconstruct(target_name: str, tool_name: str, output_dir: str):
    prefix = f"{sanitize_for_filename(tool_name)}_{sanitize_for_filename(target_name)}"

    # logs directory contains paths like this:
    # {tool name}_{target name}_{timestamp}

    # We will try to find the max timestamp to get the most recent run
    most_recent_log_path = max(f for f in os.listdir("logs") if f.startswith(prefix))

    if not most_recent_log_path:
        raise RuntimeError(f"No logs found for target {target_name} and tool {tool_name}")

    most_recent_log_path = Path(f"logs/{most_recent_log_path}")

    # All paths will be attempt_#/; get the last attempt
    last_attempt = max(f for f in os.listdir(most_recent_log_path) if f.startswith("attempt_"))
    most_recent_log_path /= last_attempt

    success = True

    if os.path.exists(f"targets/{target_name}"):
        print(f"Target {target_name} already exists, skipping reconstruction")
    else:
        success = produce_replay(
            log_dir=most_recent_log_path,
            output_dir=Path("replay"),
            attempt_number=1,
            tool_name=tool_name,
            target_name=target_name,
            require_successful_docker=True
        )

    if not success:
        raise RuntimeError(f"Failed to produce replay for target {target_name} and tool {tool_name}")

    replay_dir = Path("replay") / last_attempt

    if os.path.exists(f"targets/{target_name}"):
        return _get_checker_run_script(Path(replay_dir).absolute() / "replay.sh", tool_name)

    result = subprocess.run(["./launch.sh", "--build"], capture_output=True, text=True, check=True, cwd=replay_dir)

    match = re.search(r"^Image name: (\S+)$", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError("Could not find image name in launch.sh output")

    image_name = match.group(1)

    import docker

    client = docker.from_env()
    os.makedirs(output_dir, exist_ok=True)

    container = client.containers.run(
        image=image_name,
        detach=True,
        entrypoint=["/bin/bash", "/replay.sh"],
        volumes={
            Path(replay_dir).absolute() / "replay.sh": {
                "bind": "/replay.sh",
                "mode": "ro",
            }
        }
    )

    container.wait()

    try:
        bits, _ = container.get_archive("/app")

        tar_stream = io.BytesIO()
        for chunk in bits:
            tar_stream.write(chunk)
        tar_stream.seek(0)

        with tarfile.open(fileobj=cast(IO[bytes], tar_stream)) as tar:
            tar.extractall(path=output_dir)
    finally:
        container.remove(force=True)
        client.images.remove(image_name, force=True)

    return _get_checker_run_script(Path(replay_dir).absolute() / "replay.sh", tool_name)


def _get_checker_run_script(path_to_replay_sh: Path, tool_name: str):
    bash = Language(tree_sitter_bash.language())
    parser = Parser(bash)

    with open(path_to_replay_sh, 'rb') as f:
        source = f.read()

    tree = parser.parse(source)

    commands = []

    def visit(node):
        if node.type == 'command':
            commands.append(source[node.start_byte:node.end_byte].decode('utf-8'))
        for child in node.children:
            visit(child)

    visit(tree.root_node)

    # Last command containing the tool name will be the one to run the annotation processor
    for command in commands[::-1]:
        if tool_name in command:
            # Make everything relative, since this command is no longer being run in the Docker container
            return re.sub(r"(^|\s)/", r"\1", command, flags=re.MULTILINE)

    raise RuntimeError(f"Could not find execution of {tool_name}")
