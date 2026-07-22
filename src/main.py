from __future__ import annotations

import faulthandler
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import argparse
import checker_framework
import dotenv
import specimin
from autobuilding import enable_checkers
from java_parser import get_target_signature_and_modularity_model
from logger import FailureLogger
from more_itertools import first
from refactor_agent import RefactorAgent
from utils import run_checker_and_parse_errors, run_git_reset_hard, run_git_commit, replace_in_head_commit

faulthandler.enable()

checker_framework_url = "https://github.com/typetools/checker-framework/"


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

    success, command = enable_checkers(target.name, target.url, checkers[0], checker_framework_url)

    base_repo_dir = Path(f"targets/{target.name}")

    if not success:
        flog.logger.error("Failed to enable checkers for %s", target.name)
        print(f"Failed to enable checkers for {target.name}")
        return

    # Commit set-up repo so we don't undo these changes in git resets later
    run_git_commit(base_repo_dir)

    last_checker = None

    for checker in checkers:
        workspace_root = Path(f"workspace/{target.name}/{checker}")
        repo_dir = workspace_root / target.name

        shutil.copytree(base_repo_dir, repo_dir, dirs_exist_ok=True)
        if last_checker is not None:
            command = command.replace(last_checker, checker)
            replace_in_head_commit(search=last_checker, replace=checker, repo=repo_dir)

        errors = run_checker_and_parse_errors(command, repo_dir)

        workspace_root = Path(f"workspace/{target.name}/{checker}")
        os.makedirs(workspace_root, exist_ok=True)

        for index, error in enumerate(errors):
            print()
            print(f"Beginning processing of error {index + 1} / {len(errors)} ======>")
            flog.log_error_start(target.name, checker, index + 1, len(errors))

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

                agent = RefactorAgent(specimin_output, checker, error_transposed, other_errors, targets[0], repo_dir)
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

            finally:
                # Run git reset --hard to undo any changes made to `repo_dir` when handling this error
                run_git_reset_hard(repo_dir)

        last_checker = checker

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
        targets = [Target(**json.loads(line)) for line in f]

    dotenv.load_dotenv()

    # AnalysisAgent requires the unix shell
    ensure_posix_and_docker()
    specimin.setup()
    checker_framework.setup()

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
