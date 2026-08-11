from __future__ import annotations

import faulthandler
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from multiprocessing import Pool
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
from utils import run_checker_and_parse_errors, CheckerError, timestamp, \
    ensure_unbounded_diagnostics_and_cf_only_errors, ExcInfo, make_picklable_exc_info

DRY_RUN_MAX_ERRORS = 1

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


def _configure_multiprocessing() -> None:
    # WSL has a weird IO-related crash without this
    import multiprocessing
    try:
        if "microsoft" in platform.uname().release.lower():
            multiprocessing.set_start_method("fork", force=True)
    except Exception:
        return False


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
    exc: ExcInfo | None = None


@dataclass
class SetupFailure:
    target_name: str
    exc: ExcInfo


def _log_cf_error_refactor_result(logger: FPMinerLogger, project: TargetProject, result: ProcessResult) -> None:
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


def _get_max_processes() -> int:
    return int(os.getenv("MAX_PROCESSES", os.process_cpu_count() or 1))


def _setup_checker_for_project_star(args: tuple) -> TargetProject | SetupFailure:
    try:
        return setup_checker_for_project(*args)
    except KeyboardInterrupt:
        raise
    except Exception:
        target, _, _, _ = args
        return SetupFailure(target, make_picklable_exc_info(sys.exc_info()))


def setup_checker_for_project(target: Target, checker: str, project: TargetProject | None,
                              checker_template: str) -> TargetProject:
    if project:
        project.checkout_workspace(checker, checker_template)
        return project

    project = TargetProject(target.name, target.url, checker)
    project.compile_jar()
    return project


def run_for_single_checker_target_pair(run_id: str, project: TargetProject, checker: str, logger: FPMinerLogger,
                                       diff_tester: DifferentialTester, dry_run: bool = False):
    """Process one target repository against one checker.

    If dry_run is True, each target/checker pair is capped at
    DRY_RUN_MAX_ERRORS errors so the pipeline can be smoke-tested quickly.

    This method is not safe to parallelize. It parallelizes within."""

    logger.start_target(project.target_name, checker)
    errors = project.errors[:DRY_RUN_MAX_ERRORS] if dry_run else project.errors
    result_handler = RefactorResultHandler(run_id, project.target_name, checker)

    repo_dir = project.get_current_workspace_repo_dir()

    with Pool(processes=_get_max_processes()) as pool:
        args = [(i, e, project, repo_dir, diff_tester) for i, e in enumerate(errors)]

        try:
            for result in pool.imap_unordered(_process_one_error_star, args):
                _log_cf_error_refactor_result(logger, project, result)

                if result.refactor_run:
                    result_handler.handle_refactor_result(result.refactor_run, result.index)
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.log_crash(scope="error", target_name=project.target_name,
                             exc=make_picklable_exc_info(sys.exc_info()))

    logger.finish_target(project.target_name)


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

        checker_cmd = ensure_unbounded_diagnostics_and_cf_only_errors(
            checker_framework.get_command_for_checker(project.active_checker, specimin_output))
        errors_in_minimized = run_checker_and_parse_errors(checker_cmd, specimin_output)

        assert errors_in_minimized is not None

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

    except KeyboardInterrupt:
        raise
    except Exception:
        return ProcessResult(index=index + 1, outcome="crash", exc=make_picklable_exc_info(sys.exc_info()))


def main():
    """Parse CLI inputs and execute the false-positive mining pipeline."""
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--checkers", type=str, help="Path to the file containing the list of checkers to run")
    parser.add_argument("-t", "--targets", type=str,
                        help="Path to the file containing the list of repositories to run on")
    parser.add_argument("--dry-run", action="store_true",
                        help=f"Limit each target/checker pair to at most {DRY_RUN_MAX_ERRORS} errors, for a quick smoke test")
    parser.add_argument("--setup-only", action="store_true",
                        help=f"Run the checker setup step only for each target project, without running the full pipeline")

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
        checkers = [line.strip() for line in f.readlines() if line]

    with open(args.targets) as f:
        targets = [Target(**json.loads(line)) for line in f if line.strip() and not line.startswith("//")]

    dotenv.load_dotenv()

    # AnalysisAgent requires the unix shell + docker
    ensure_posix_and_docker()

    run_id = timestamp()
    logger = FPMinerLogger(run_id)

    try:
        specimin.setup()
        checker_framework.setup()
        differential_tester.setup()
        _configure_multiprocessing()
    except Exception:
        logger.log_crash(scope="pipeline_setup", target_name="<setup>", exc=make_picklable_exc_info(sys.exc_info()))
        logger.finalize()
        exit(1)

    projects: dict[str, TargetProject] = {}
    diff_testers: dict[str, DifferentialTester] = {}

    # Parallelize checker setup, then parallelize errors in each target/checker pair
    for checker in checkers:
        logger.start_checker(checker)

        with Pool(processes=_get_max_processes()) as pool:
            setup_args = [(target, checker, projects.get(target.name, None), checkers[0]) for target in targets]

            for project in pool.imap_unordered(_setup_checker_for_project_star, setup_args):
                if isinstance(project, SetupFailure):
                    logger.log_crash(scope="target_setup", target_name=project.target_name, exc=project.exc)
                    continue

                logger.log_setup_method(project.target_name, checker, project.checker_setup_type)
                logger.log_total_errors(project.target_name, checker, len(project.errors))

                curr_proj = projects.get(project.target_name, None)

                if not curr_proj:
                    projects[project.target_name] = project

                    try:
                        diff_testers[project.target_name] = DifferentialTester(project.jar_path, project.base_dir,
                                                                               project.target_name)
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        logger.log_crash(scope="target_setup", target_name=project.target_name,
                                         exc=make_picklable_exc_info(sys.exc_info()))

        if not args.setup_only:
            for target in targets:
                try:
                    run_for_single_checker_target_pair(run_id, projects[target.name], checker, logger,
                                                       diff_testers[target.name],
                                                       dry_run=args.dry_run)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logger.log_crash(scope="target", target_name=target.name,
                                     exc=make_picklable_exc_info(sys.exc_info()))

        logger.finish_checker(checker)

    logger.finalize()


@dataclass
class Target:
    """Input row describing a repository target to process."""
    name: str
    url: str


if __name__ == "__main__":
    main()
