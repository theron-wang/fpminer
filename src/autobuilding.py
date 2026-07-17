import io
import json
import os
import re
import shlex
import subprocess
import tarfile
from pathlib import Path
from typing import IO, cast

import tree_sitter_bash
from analysis_agent.mini_orchestrator import sanitize_for_filename, run_with_attempts
from analysis_agent.replay_producer import produce_replay
from checker_framework import get_path_to_checker_jar, get_path_to_checker_dir, get_path_to_dljc, get_javac_path
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models.litellm_model import LitellmModel
from tree_sitter import Language, Parser
from utils import run_checker_and_parse_errors

LOGS_ROOT = Path("analysis_agent_logs")

DLJC_BUILD_COMMANDS = [
    ["./gradlew", "compileJava", "--rerun-tasks"],
    ["gradle", "compileJava", "--rerun-tasks"],
    ["./mvnw", "compile"],
    ["mvn", "compile"],
    ["ant", "compile"],
]


# Available models:
# gemini/gemini-3-flash-preview
# gemini/gemini-3.5-flash
# gemini/gemini-2.5-flash
# gemini/gemini-2.5-flash-lite
# gemini/gemini-3.1-flash-lite

def enable_checkers(target_name: str, target_url: str, tool_name: str, tool_url: str) -> tuple[bool, str]:
    print(f"Running do-like-javac on {target_name} with tool {tool_name}")
    cmd_from_dljc = _try_do_like_javac(target_name, target_url, tool_name)

    if cmd_from_dljc:
        print("do-like-javac successful. Skipping AnalysisAgent.")
        return True, cmd_from_dljc

    success = True

    print(f"Running AnalysisAgent on {target_name} with tool {tool_name}")

    if os.path.exists(f"targets/{target_name}"):
        print(f"Target {target_name} already exists, skipping")
    else:
        model = LitellmModel(model_name=os.environ["EXEC_AGENT_MODEL"])
        env = DockerEnvironment(
            image="ubuntu:22.04"
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
            enable_exit_attempt=False,
            logs_root=LOGS_ROOT
        )

    if not success:
        return False, None

    # AnalysisAgent automatically cleans up the Docker container after execution,
    # so we need to reconstruct the output
    print(f"Reconstructing output for target {target_name} with tool {tool_name}")
    command = _reconstruct(target_name, tool_name, f"targets/{target_name}")
    print("Reconstruction complete.")
    print()

    return True, command


def _try_do_like_javac(target_name: str, target_url: str, tool_name: str):
    if os.path.exists(f"targets/{target_name}"):
        print(f"Target {target_name} already exists, skipping cloning")
    else:
        _clone_target(target_name, target_url)

    # dljc needs this
    os.environ.setdefault("CHECKERFRAMEWORK", str(get_path_to_checker_dir().resolve()))

    dljc_path = get_path_to_dljc()

    for build_command in DLJC_BUILD_COMMANDS:
        dljc_cmd = shlex.join(
            [str(dljc_path.resolve()), "--lib", str(get_path_to_checker_jar().resolve()), "-t", "print",
             "--checker", tool_name, "--jdkVersion", "17", "--"] + build_command)

        javac_commands = _run_dljc_print(dljc_cmd, f"targets/{target_name}")

        if not javac_commands:
            continue

        output_cmd = " ; ".join([
            _build_javac_command(javac_command, tool_name)
            for javac_command in javac_commands
        ])

        errors = run_checker_and_parse_errors(output_cmd, Path(f"targets/{target_name}"))

        if errors:
            return output_cmd

    return None


def _run_dljc_print(dljc_cmd: str, cwd: str) -> list[dict] | None:
    result = subprocess.run(dljc_cmd, cwd=cwd, capture_output=True, text=True, check=False, shell=True)
    json_start = result.stdout.find("{")
    if json_start == -1:
        return None
    try:
        parsed = json.loads(result.stdout[json_start:])
    except json.JSONDecodeError:
        return None
    return parsed.get("javac_commands", [])


def _build_javac_command(javac_command: dict, tool_name: str) -> str:
    switches = javac_command["javac_switches"]
    java_files = javac_command["java_files"]

    cmd = [str(get_javac_path().resolve()), "-processor", tool_name]

    for key, value in switches.items():
        if key in ("proc:none", "h"):
            # proc:none conflicts with -processor; -h (native headers) isn't needed here.
            continue
        flag = f"-{key}"
        if value is True:
            cmd.append(flag)
        else:
            cleaned = value.strip('"') if isinstance(value, str) else value
            cmd.extend([flag, str(cleaned)])

    cmd.extend(java_files)

    return shlex.join(cmd)


def _run_dljc(dljc_cmd: str, checker_bin_javac: Path, tool_name: str) -> str:
    javac_commands = _run_dljc_print(dljc_cmd)
    outputs = [
        _build_javac_command(javac_command, checker_bin_javac, tool_name)
        for javac_command in javac_commands
    ]
    return "\n".join(outputs)


def _clone_target(target_name: str, target_url: str):
    clone_to = Path("targets") / target_name

    subprocess.run(
        ["git", "clone", target_url, clone_to, "-b", "master", "--depth", "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )


def _reconstruct(target_name: str, tool_name: str, output_dir: str):
    prefix = f"{sanitize_for_filename(tool_name)}_{sanitize_for_filename(target_name)}"

    # logs directory contains paths like this:
    # {tool name}_{target name}_{timestamp}

    # We will try to find the max timestamp to get the most recent run
    most_recent_log_path = max(f for f in os.listdir(LOGS_ROOT) if f.startswith(prefix))

    if not most_recent_log_path:
        raise RuntimeError(f"No logs found for target {target_name} and tool {tool_name}")

    most_recent_log_path = LOGS_ROOT / most_recent_log_path

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
