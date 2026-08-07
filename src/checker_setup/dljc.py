import json
import os
import shlex
import subprocess
from pathlib import Path

from checker_framework import get_path_to_checker_jar, get_path_to_checker_dir, get_path_to_dljc, get_javac_path
from utils import run_checker_and_parse_errors, CheckerError, ensure_unbounded_diagnostics_and_cf_only_errors

# DLJC cannot have compile be cached
DLJC_BUILD_COMMANDS = [
    ["./gradlew", "compileJava", "--rerun-tasks"],
    ["./mvnw", "clean", "compile", "-Dmaven.compiler.fork=true"]
]


def run_dljc(target_name: str, target_url: str, tool_name: str) -> list[CheckerError] | None:
    """
    Runs dljc on the given target project and checker. Returns a list (could be empty) of errors
    if dljc ran successfully, or None if it failed.
    :param target_name: The target name
    :param target_url: The target url
    :param tool_name: The tool name
    :return: A list of errors, or None if unsuccessful
    """
    if not os.path.exists(f"targets/{target_name}"):
        _clone_target(target_name, target_url)

    # dljc needs this
    os.environ.setdefault("CHECKERFRAMEWORK", str(get_path_to_checker_dir().resolve()))

    dljc_path = get_path_to_dljc()

    for build_command in DLJC_BUILD_COMMANDS:
        dljc_cmd = shlex.join(
            [str(dljc_path.resolve()), "--lib", str(get_path_to_checker_jar().resolve()), "-t", "print",
             "--checker", tool_name, "--"] + build_command)

        javac_commands = _run_dljc_print(dljc_cmd, f"targets/{target_name}")

        if not javac_commands:
            continue

        output_cmd = " ; ".join([
            ensure_unbounded_diagnostics_and_cf_only_errors(_build_javac_command(javac_command, tool_name))
            for javac_command in javac_commands
        ])

        errors = run_checker_and_parse_errors(output_cmd, Path(f"targets/{target_name}"))

        if errors is None:
            continue

        return errors

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
        ["git", "clone", target_url, str(clone_to), "--depth", "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )
