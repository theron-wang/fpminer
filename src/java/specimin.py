import os
import shlex
import subprocess
from pathlib import Path

from utils import CheckerError

SPECIMIN_URL = "https://github.com/njit-jerse/specimin.git"
DOWNLOAD_TO = Path("tools/specimin")


def setup():
    if os.getenv("SPECIMIN"):
        print("Specimin already exists: using local copy")
        return

    if os.path.exists(DOWNLOAD_TO):
        print("Specimin already exists: pulling most recent changes")
        subprocess.run(
            ["git", "pull"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=DOWNLOAD_TO)
        return
    print("Cloning Specimin from GitHub")
    subprocess.run(
        ["git", "clone", SPECIMIN_URL, DOWNLOAD_TO, "--depth", "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )


def _guess_root(error: CheckerError, targets: list[str]):
    target_fqn = targets[0]
    # The error file path will likely contain some portion of the target's fully qualified name
    target_fqn_segments = target_fqn.split("#")[0].split('.')

    fqn_segment = 0
    matching_path = Path(target_fqn_segments[fqn_segment])
    actual_matching_path = None

    # Normalize in case anything is weird
    error_file_path = str(Path(error.file_path))

    while str(matching_path) in error_file_path:
        actual_matching_path = matching_path

        if fqn_segment + 1 >= len(target_fqn_segments):
            break

        fqn_segment += 1
        matching_path /= target_fqn_segments[fqn_segment]

    assert actual_matching_path

    return error_file_path[0:error_file_path.rindex(str(actual_matching_path))]


def minimize(error: CheckerError, targets: list[str], nullaway: bool, target_project_dir: Path, output_dir: Path):
    """Runs Specimin to minimize the given error.

    Returns (success, cmd), where `cmd` is the exact Specimin invocation that
    was run. The caller is responsible for logging failures (via
    FailureLogger.log_failed_minimization) using the returned `cmd` — this
    keeps all failure logging centralized in one place instead of scattered
    across modules.
    """
    root = _guess_root(error, targets)

    specimin_args = [
        "--outputDirectory", str(output_dir.absolute()),
        "--root", str((target_project_dir / root).absolute()),
        "--targetFile", str(Path(error.file_path).relative_to(root))]

    if nullaway:
        specimin_args.append("--modularityModel")
        specimin_args.append("nullaway")

    for target in targets:
        is_method = "(" in target
        if is_method:
            specimin_args.append("--targetMethod")
        else:
            specimin_args.append("--targetField")
        specimin_args.append(target)

    specimin_args_as_str = shlex.join(specimin_args)

    cmd = shlex.join(["./gradlew", "run", f"--args={specimin_args_as_str}"])
    if os.path.exists(output_dir):
        print("Already minimized. Skipping.")
        return True, cmd

    result = subprocess.run(["./gradlew", "run", f"--args={specimin_args_as_str}", "-PskipCheckerFramework"],
                            cwd=os.getenv("SPECIMIN") or DOWNLOAD_TO,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if result.returncode != 0:
        return False, cmd
    return True, cmd
