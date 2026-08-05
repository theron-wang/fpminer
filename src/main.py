from __future__ import annotations

import faulthandler
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import argparse
import checker_framework
import differential_tester
import dotenv
from differential_tester import DifferentialTester
from java import specimin
from java.java_parser import get_target_signature_and_modularity_model
from logger import FPMinerLogger
from more_itertools import first
from refactoring.refactor_agent import RefactorAgent, RefactorAgentRun
from refactoring.refactor_result_handler import RefactorResultHandler
from target_project import TargetProject
from utils import run_checker_and_parse_errors, CheckerError, timestamp

faulthandler.enable()


def ensure_posix_and_docker():
    """Validate host assumptions required by the pipeline."""
    if os.name != 'posix':
        print(
            "Error: This script must be run on a POSIX-compliant operating system (e.g., Linux, macOS). If using Windows, use WSL.")
        exit(1)

    try:
        subprocess.run(["docker", "version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: Docker is not installed, not in the PATH, or the engine is not running.")
        exit(1)


@dataclass
class ProcessResult:
    """Picklable summary of one process_one_error() run.

    Workers never touch FPMinerLogger (its `_lock` can't be pickled), so
    each worker returns one of these instead, and the main process turns it
    into the appropriate logger.log_*() call(s) via _log_result().
    """
    index: int
    outcome: Literal["skipped", "failed_minimization", "failed_compilation", "failed_preservation", "success", "crash"]
    targets_fields: list[str] | None = None
    specimin_cmd: str = ""
    errors_in_minimized: list[CheckerError] | None = None
    expected_error: CheckerError | None = None
    refactor_run: RefactorAgentRun | None = None
    exc: BaseException | None = None


def _log_result(logger: FPMinerLogger, project: TargetProject, result: ProcessResult) -> None:
    """Runs only in the main process. Dispatches a completed worker's
    ProcessResult to the matching FPMinerLogger call(s), as each result
    streams back from the pool -- so logging happens as errors finish
    processing, not all at once at the end."""
    target_name = project.target_name
    checker = project.active_checker
    index = result.index

    # Preserves the original per-error "start" log entry / counter increment;
    # it now fires when a result comes back rather than before it's dispatched,
    # since imap_unordered doesn't give us a hook before work begins.
    logger.log_error_start(target_name, checker, index, len(project.errors))

    if result.outcome == "skipped":
        logger.log_skipped_error(target_name, result.targets_fields or [], checker, index)
    elif result.outcome == "failed_minimization":
        logger.log_failed_minimization(target_name, checker, index, result.specimin_cmd)
    elif result.outcome == "failed_compilation":
        logger.log_failed_compilation(target_name, checker, index, result.specimin_cmd,
                                      result.errors_in_minimized)
    elif result.outcome == "failed_preservation":
        logger.log_failed_preservation(target_name, checker, index, result.specimin_cmd,
                                       result.expected_error, result.errors_in_minimized)
    elif result.outcome == "success":
        assert result.refactor_run is not None
        logger.log_refactor_result(target_name, checker, index, result.refactor_run)
        logger.log_success(target_name, checker, index)
    elif result.outcome == "crash":
        assert result.exc is not None
        logger.log_crash(scope="error", target_name=target_name, exc=result.exc,
                         checker=checker, index=index)
    else:
        logger.logger.error("Unknown ProcessResult outcome %r for error %d", result.outcome, index)


def run(run_id: str, target: Target, checkers: list[str], logger: FPMinerLogger):
    """Process one target repository against all configured checkers."""
    logger.start_target(target.name)

    try:
        project = TargetProject(target.name, target.url, checkers[0])
    except ValueError as exc:
        logger.logger.error("Failed to enable checkers for %s: %s", target.name, exc)
        return

    jar_path = project.compile_jar()
    diff_tester = DifferentialTester(jar_path, project.base_dir, target.name)

    for checker in checkers:
        logger.start_checker(target.name, checker)
        repo_dir = project.checkout_workspace(checker, checkers[0])
        logger.log_setup_method(target.name, checker, project.checker_setup_type)
        result_handler = RefactorResultHandler(run_id, target.name, checker)
        with Pool(processes=int(os.getenv("MAX_PROCESSES", os.cpu_count() or 1))) as pool:
            args = [(i, e, project, repo_dir, diff_tester) for i, e in enumerate(project.errors)]

            for result in pool.imap_unordered(_process_one_error_star, args):
                _log_result(logger, project, result)

                if result.refactor_run:
                    result_handler.handle_refactor_result(result.refactor_run, result.index)
        logger.finish_checker(checker)

    logger.finish_target(target.name)


def _process_one_error_star(args: tuple) -> ProcessResult:
    return process_one_error(*args)


def process_one_error(index: int, error: CheckerError, project: TargetProject, repo_dir: Path,
                      diff_tester: DifferentialTester) -> ProcessResult:
    try:
        targets, nullaway = get_target_signature_and_modularity_model(repo_dir, error)

        if not any(t for t in targets if '(' in t):
            # Targeting only fields means that the code is likely not interesting.
            return ProcessResult(index=index + 1, outcome="skipped", targets_fields=targets)

        specimin_output = repo_dir.parent / str(index + 1) / "orig"
        min_successful, executed_specimin_cmd = specimin.minimize(
            error, targets, nullaway, repo_dir, specimin_output
        )

        if not min_successful:
            return ProcessResult(index=index + 1, outcome="failed_minimization",
                                 specimin_cmd=executed_specimin_cmd)

        checker_cmd = checker_framework.get_command_for_checker(project.active_checker, specimin_output)
        errors_in_minimized = run_checker_and_parse_errors(checker_cmd, specimin_output)

        error_transposed = first((e for e in errors_in_minimized if e.likely_equals(error)), None)

        if any(e.is_compilation_error() for e in errors_in_minimized):
            return ProcessResult(index=index + 1, outcome="failed_compilation",
                                 specimin_cmd=executed_specimin_cmd,
                                 errors_in_minimized=errors_in_minimized)
        elif not errors_in_minimized or not error_transposed:
            return ProcessResult(index=index + 1, outcome="failed_preservation",
                                 specimin_cmd=executed_specimin_cmd,
                                 expected_error=error,
                                 errors_in_minimized=errors_in_minimized)

        other_errors = [e for e in errors_in_minimized if not e.likely_equals(error)]

        agent = RefactorAgent(specimin_output, project.active_checker, error_transposed, other_errors, targets[0],
                              repo_dir,
                              diff_tester)
        result = agent.run()

        return ProcessResult(index=index + 1, outcome="success",
                             specimin_cmd=executed_specimin_cmd, refactor_run=result)

    except Exception as exc:
        return ProcessResult(index=index + 1, outcome="crash", exc=exc)


def main():
    """Parse CLI inputs and execute the false-positive mining pipeline."""
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
        targets = [Target(**json.loads(line)) for line in f if line.strip() and not line.startswith("//")]

    dotenv.load_dotenv()

    # AnalysisAgent requires the unix shell + docker
    ensure_posix_and_docker()
    specimin.setup()
    checker_framework.setup()
    differential_tester.setup()

    run_id = timestamp()
    logger = FPMinerLogger(run_id)

    for target in targets:
        try:
            run(run_id, target, checkers, logger)
        except Exception as exc:
            logger.log_crash(scope="target", target_name=target.name, exc=exc)

    logger.finalize()


@dataclass
class Target:
    """Input row describing a repository target to process."""
    name: str
    url: str


if __name__ == "__main__":
    main()
