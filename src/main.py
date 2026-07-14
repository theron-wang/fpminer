from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import argparse
import checker_framework
import dotenv
import specimin
from autobuilding import enable_checkers
from java_parser import get_target_signature_and_modularity_model
from utils import run_checker_and_parse_errors

checker_framework_url = "https://github.com/typetools/checker-framework/"


def ensure_posix_and_docker():
    if os.name != 'posix':
        print(
            "Error: This script must be run on a POSIX-compliant operating system (e.g., Linux, macOS). If using Windows, use WSL.")
        exit(1)

    try:
        subprocess.run(["docker", "version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: Docker is not installed, not in the PATH, or the engine is not running.")
        exit(1)


def run(target: Target, checkers: list[str]):
    # use the first checker; we will modify the resulting dockerfile for the other checkers
    success, command = enable_checkers(target.name, target.url, checkers[0], checker_framework_url)

    repo_dir = Path(f"targets/{target.name}")

    if not success:
        print(f"Failed to enable checkers for {target.name}")
        return

    errors = run_checker_and_parse_errors(command, repo_dir)

    specimin_output_base = Path(f"workspace/{target.name}/{checkers[0]}")
    os.makedirs(specimin_output_base, exist_ok=True)

    for index, error in enumerate(errors):
        print()
        print(f"Beginning processing of error {index + 1} / {len(errors)} ======>")

        targets, nullaway = get_target_signature_and_modularity_model(repo_dir, error)

        specimin_output = specimin_output_base / str(index + 1) / "orig"
        min_successful, executed_specimin_cmd = specimin.minimize(error, targets, nullaway, repo_dir, specimin_output)

        if min_successful:
            print("Minimization successful. Proceeding to refactoring.")
        else:
            print("Minimization failed. See failed_minimizations.txt.")
            print(f"<====== Processing of error {index + 1} complete")
            continue

        checker_cmd = checker_framework.get_command_for_checker(checkers[0], specimin_output)
        errors_in_minimized = run_checker_and_parse_errors(checker_cmd, specimin_output)

        if any([e.is_compilation_error() for e in errors_in_minimized]):
            specimin.add_to_failed_compilations(specimin_output / "../", errors_in_minimized, executed_specimin_cmd)
            print("Minimization failed to produce compilable output. See failed_compilations.txt.")
            print(f"<====== Processing of error {index + 1} complete")
            continue
        elif not errors_in_minimized or not (any([e.likely_equals(error) for e in errors_in_minimized])):
            specimin.add_to_failed_preservations(specimin_output / "../", error, errors_in_minimized,
                                                 executed_specimin_cmd)
            print("Minimization failed to preserve. See failed_preservations.txt.")
            print(f"<====== Processing of error {index + 1} complete")
            continue

        other_errors = [e for e in errors_in_minimized if not e.likely_equals(error)]

        # agent = RefactorAgent(specimin_output, checker_cmd, checkers[0], other_errors)

        print(f"<====== Processing of error {index + 1} complete")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--checkers", type=str, help="Path to the file containing the list of checkers to run")
    parser.add_argument("-t", "--targets", type=str,
                        help="Path to the file containing the list of repositories to run on")

    args = parser.parse_args()

    if args.checkers is None:
        print(
            "Error: Please provide a path to the file containing the list of checkers to run using the -c or --checkers option.")
        exit(1)

    if not os.path.exists(args.checkers):
        print("Error: The specified checkers file does not exist.")
        exit(1)

    if args.targets is None:
        print(
            "Error: Please provide a path to the file containing the list of repositories to run on using the -t or --targets option.")
        exit(1)

    if not os.path.exists(args.targets):
        print("Error: The specified targets file does not exist.")
        exit(1)

    with open(args.checkers) as f:
        checkers = [line.strip() for line in f.readlines()]

    with open(args.targets) as f:
        targets = [Target(**json.loads(line)) for line in f]

    dotenv.load_dotenv()

    # AnalysisAgent requires the unix shell
    ensure_posix_and_docker()
    specimin.setup()
    checker_framework.setup()

    for target in targets:
        print(f"============================ Running for {target.name} ============================")
        print()
        run(target, checkers)
        print(f"============================ Finished run for {target.name} ============================")
        print()


@dataclass
class Target:
    name: str
    url: str


if __name__ == "__main__":
    main()
