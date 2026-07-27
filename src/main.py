from __future__ import annotations

import faulthandler
import json
import os
from dataclasses import dataclass

import argparse
import checker_framework
import differential_tester
import dotenv
from differential_tester import DifferentialTester
from logger import FailureLogger
from more_itertools import first
from refactoring.refactor_agent import RefactorAgent
from specimin import specimin
from specimin.java_parser import get_target_signature_and_modularity_model
from target_project import TargetProject
from utils import run_checker_and_parse_errors

faulthandler.enable()


def ensure_posix_and_docker():
    if os.name != 'posix':
        print(
            "Error: This script must be run on a POSIX-compliant operating system (e.g., Linux, macOS). If using Windows, use WSL.")
        exit(1)

    # try:
    #     subprocess.run(["docker", "version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # except (subprocess.CalledProcessError, FileNotFoundError):
    #     print("Error: Docker is not installed, not in the PATH, or the engine is not running.")
    #     exit(1)


def run(target: Target, checkers: list[str], flog: FailureLogger):
    flog.start_target(target.name)

    try:
        project = TargetProject(target.name, target.url)
    except ValueError as exc:
        flog.logger.error("Failed to enable checkers for %s: %s", target.name, exc)
        print(f"Failed to enable checkers for {target.name}")
        return

    jar_path = project.compile_jar()
    diff_tester = DifferentialTester(jar_path, project.base_dir, target.name)

    for checker in checkers:
        repo_dir = project.checkout_workspace(checker, checkers[0])

        workspace_root = repo_dir.parent

        for index, error in enumerate(project.errors):
            print()
            print(f"Beginning processing of error {index + 1} / {len(project.errors)} ======>")
            flog.log_error_start(target.name, checker, index + 1, len(project.errors))

            try:
                targets, nullaway = get_target_signature_and_modularity_model(repo_dir, error)

                if not any(t for t in targets if '(' in t):
                    # Targeting only fields means that the code is likely not interesting.
                    print(f"Error {index + 1} only targets fields. Skipping.")
                    flog.log_skipped_error(target.name, targets, checker, index + 1)
                    print(f"<====== Processing of error {index + 1} complete")
                    continue

                specimin_output = workspace_root / str(index + 1) / "orig"
                min_successful, executed_specimin_cmd = specimin.minimize(
                    error, targets, nullaway, repo_dir, specimin_output
                )

                if min_successful:
                    print("Minimization successful. Proceeding to refactoring.")
                else:
                    flog.log_failed_minimization(target.name, checker, index + 1, executed_specimin_cmd)
                    print("Minimization failed. See failed_minimizations.jsonl in the run's log directory.")
                    print(f"<====== Processing of error {index + 1} complete")
                    continue

                checker_cmd = checker_framework.get_command_for_checker(checker, specimin_output)
                errors_in_minimized = run_checker_and_parse_errors(checker_cmd, specimin_output)

                error_transposed = first((e for e in errors_in_minimized if e.likely_equals(error)), None)

                if any(e.is_compilation_error() for e in errors_in_minimized):
                    flog.log_failed_compilation(target.name, checker, index + 1,
                                                executed_specimin_cmd, errors_in_minimized)
                    print(
                        "Minimization failed to produce compilable output. See failed_compilations.jsonl in the run's log directory.")
                    print(f"<====== Processing of error {index + 1} complete")
                    continue
                elif not errors_in_minimized or not error_transposed:
                    flog.log_failed_preservation(target.name, checker, index + 1,
                                                 executed_specimin_cmd, error, errors_in_minimized)
                    print("Minimization failed to preserve. See failed_preservations.jsonl in the run's log directory.")
                    print(f"<====== Processing of error {index + 1} complete")
                    continue

                other_errors = [e for e in errors_in_minimized if not e.likely_equals(error)]

                agent = RefactorAgent(specimin_output, checker, error_transposed, other_errors, targets[0], repo_dir,
                                      diff_tester)
                result = agent.run()

                flog.log_refactor_result(target.name, checker, index + 1, result, executed_specimin_cmd)

                flog.log_success(target.name, checker, index + 1)
                print(f"<====== Processing of error {index + 1} complete")

            except Exception as exc:
                flog.log_crash(scope="error", target_name=target.name, exc=exc,
                               checker=checker, index=index + 1)
                print(f"Unhandled exception while processing error {index + 1}: {exc}. See crashes.jsonl. Continuing.")
                print(f"<====== Processing of error {index + 1} complete (with exception)")
                continue

    flog.finish_target(target.name)


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
        targets = [Target(**json.loads(line)) for line in f if not line.startswith("//")]

    dotenv.load_dotenv()

    # AnalysisAgent requires the unix shell + docker
    ensure_posix_and_docker()
    specimin.setup()
    checker_framework.setup()
    differential_tester.setup()

    flog = FailureLogger()

    for target in targets:
        print(f"============================ Running for {target.name} ============================")
        print()

        try:
            run(target, checkers, flog)
        except Exception as exc:
            flog.log_crash(scope="target", target_name=target.name, exc=exc)
            print(f"Unhandled exception while running target {target.name}: {exc}. See crashes.jsonl. Continuing.")

        print(f"============================ Finished run for {target.name} ============================")
        print()

    flog.finalize()


@dataclass
class Target:
    name: str
    url: str


if __name__ == "__main__":
    main()
