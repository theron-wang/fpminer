from __future__ import annotations

import logging
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Optional, Literal

from refactoring.refactor_agent import RefactorAgentRun
from utils import CheckerError, timestamp

LOGGER_LEVEL = logging.INFO

_SEPARATOR = "=" * 80

# Failure types recorded in specimin_failures.log. Kept as a closed set so the
# field is a predictable enum for downstream parsing rather than free text.
FailureType = Literal["minimization", "compilation", "preservation"]


def _new_target_checker_counts() -> dict[str, Any]:
    """Default (zeroed) summary counters for a single (target, checker) pair."""
    return {
        "setup_method": None,
        "total_errors": 0,
        "specimin_errors": {
            "minimization": 0,
            "compilation": 0,
            "preservation": 0,
        },
        "refactor_agent_ran": 0,
        "refactor_success": {
            "annotation_only": 0,
            "real_changes": 0,
        },
        "refactor_failure": {
            "annotation_only": 0,
            "real_changes": 0,
        },
        "refactor_errored": 0,
        "refactor_not_possible": 0,
    }


class FPMinerLogger:
    """
    Centralized, file-based logger for the fpminer pipeline.

    Creates a per-run directory under `results/run_{run_id}/` containing exactly four
    files:

      - run.log               human-readable log of everything (DEBUG+ to
                               file, INFO+ to console), including full
                               tracebacks. Standard `logging`-formatted lines:
                               "<timestamp> | <LEVEL>   | <message>".

      - crashes.log            one fixed-format block per unhandled exception
                                caught by a try/except guard in the pipeline
                                (per-error or per-target granularity). Blocks
                                use a `Field: value` layout with a fixed field
                                order, separated by a line of dashes, so they
                                are both readable and trivially parseable
                                (split on the separator, then split each line
                                on the first ": ").

      - specimin_failures.log  one fixed-format block per failed
                                minimization/compilation/preservation. Each
                                block records the target/checker/error index,
                                the exact Specimin (`./gradlew run --args=...`)
                                command, the failure type (`minimization` —
                                i.e. a Specimin crash, `compilation`, or
                                `preservation`), and any relevant error text
                                for that failure type. Same block format as
                                crashes.log.

      - summary.json           aggregate counts, written by finalize().
                                Keyed by target, then checker. See
                                `_new_target_checker_counts()` for the exact
                                shape of each (target, checker) entry. Each
                                entry also carries a `setup_method` field
                                ("dljc" or "analysisagent", or null if never
                                recorded) set via `log_setup_method()`.

    Note: refactor-agent outcomes (the actual diffs/error text/reasoning for
    a given fix attempt) are tracked and surfaced elsewhere in the pipeline
    (`RefactorResultHandler`) and are intentionally NOT duplicated here —
    this logger only keeps aggregate pass/fail/not-possible counts for them.

    All logging calls are defensive: if writing a log record itself fails,
    that failure is swallowed (and reported via the text logger) rather than
    being allowed to crash the pipeline.

    Thread-safety: a single instance-level lock guards both the `_counts`
    aggregate dict and every file write (`_append_block`, `finalize`).
    Multiple threads may safely share one `FPMinerLogger` instance and call
    any of its methods concurrently. `self.logger` itself is not
    additionally guarded — the standard `logging` module already serializes
    handler emission internally, so it's safe to call from multiple threads
    on its own.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.run_dir = Path("results") / f"run_{self.run_id}"
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()

        # target -> checker -> counts dict (see _new_target_checker_counts)
        self._counts: dict[str, dict[str, dict[str, Any]]] = {}

        self.logger = self._build_logger()
        self.logger.info("Logging initialized. Run directory: %s", self.run_dir)

    # ------------------------------------------------------------------ #
    # setup
    # ------------------------------------------------------------------ #
    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"fp_miner.{self.run_id}")
        logger.setLevel(LOGGER_LEVEL)
        logger.propagate = False

        # Avoid duplicate handlers if a FPMinerLogger is somehow constructed
        # twice with the same run_id (e.g. re-entrant calls).
        if logger.handlers:
            return logger

        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )

        file_handler = logging.FileHandler(self.run_dir / "run.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)

        return logger

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #
    def _get_counts(self, target_name: str, checker: str) -> dict[str, Any]:
        """Must be called while holding self._lock."""
        return (
            self._counts
            .setdefault(target_name, {})
            .setdefault(checker, _new_target_checker_counts())
        )

    def _bump(self, target_name: str, checker: str, *path: str, amount: int = 1) -> None:
        """Increments a (possibly nested) counter for a (target, checker) pair.

        `path` is one or two keys into that pair's counts dict, e.g.
        ("total_errors",) or ("specimin_errors", "minimization").
        """
        with self._lock:
            counts = self._get_counts(target_name, checker)
            node = counts
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] += amount

    def _append_block(self, filename: str, fields: list[tuple[str, Optional[str]]]) -> None:
        """Appends one fixed-format, human-readable-and-parseable block to
        `filename`. `fields` is an ordered list of (label, value) pairs;
        entries whose value is None/empty are still written (with an empty
        value) so the field order and presence stay predictable for parsing
        -- only entirely absent optional text is skipped upstream by callers
        passing None for it, which renders as `Label:` with nothing after it.

        Multi-line values (tracebacks, raw checker error text) are indented
        two spaces so a block can be told apart from the next field by
        indentation, while still being trivially machine-parseable: split
        the file on lines of exactly `_SEPARATOR`, then within a block split
        each unindented line on the first ": ".
        """
        path = self.run_dir / filename
        lines: list[str] = []
        for label, value in fields:
            text = "" if value is None else str(value)
            if "\n" in text:
                lines.append(f"{label}:")
                for line in text.rstrip("\n").split("\n"):
                    lines.append(f"  {line}")
            else:
                lines.append(f"{label}: {text}")
        lines.append(_SEPARATOR)
        block = "\n".join(lines) + "\n"
        try:
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(block)
        except Exception:
            self.logger.exception("Failed to write log record to %s", path)

    # ------------------------------------------------------------------ #
    # target / checker / error lifecycle
    # ------------------------------------------------------------------ #
    def start_checker(self, checker: str) -> None:
        self.logger.info("%s Starting checker: %s %s", "=" * 20, checker, "=" * 20)

    def finish_checker(self, checker: str) -> None:
        self.logger.info("%s Finished checker: %s %s", "=" * 20, checker, "=" * 20)

    def start_target(self, target_name: str, checker: str) -> None:
        self._bump(target_name, checker, "total_errors", amount=0)
        self.logger.info("%s Starting target: %s %s", "=" * 20, target_name, "=" * 20)

    def finish_target(self, target_name: str) -> None:
        self.logger.info("%s Finished target: %s %s", "=" * 20, target_name, "=" * 20)

    def log_setup_method(self, target_name: str, checker: str, method: str) -> None:
        """Records how this (target, checker) pair's environment was set up
        ("dljc" or "analysisagent"). Overwrites any previously recorded
        value for the pair, so it's safe to call again if the setup path is
        retried with a different method. Surfaced in summary.json as the
        pair's `setup_method` field.
        """
        with self._lock:
            counts = self._get_counts(target_name, checker)
            counts["setup_method"] = method
        self.logger.info("[%s/%s] Setup complete using: %s", target_name, checker, method)

    def log_total_errors(self, target_name: str, checker: str, total: int) -> None:
        """Records the total number of errors to be processed for this
        (target, checker) pair. Intended to be called once, before any
        per-error processing begins (e.g. right after `start_checker`).

        Overwrites any previously recorded value if called again -- callers
        that only expect this to run once should treat a second call as a
        bug on their end, but this method itself does not enforce
        single-call semantics or warn on overwrite.
        """
        with self._lock:
            counts = self._get_counts(target_name, checker)
            counts["total_errors"] = total
        self.logger.info("[%s/%s] Total errors to process: %d", target_name, checker, total)

    def log_error_start(self, target_name: str, checker: str, index: int, total: int) -> None:
        self.logger.info("[%s/%s] Processing error %d/%d", target_name, checker, index, total)

    def log_success(self, target_name: str, checker: str, index: int) -> None:
        self.logger.info("[%s/%s] Error %d: processed successfully.", target_name, checker, index)

    def log_skipped_error(self, target_name: str, targets_fields: list[str], checker: str, index: int):
        """Targets-only-fields errors are counted in total_errors (already
        incremented by log_error_start) but are otherwise silently skipped:
        no dedicated summary bucket or log file entry, just a debug trace in
        run.log for local troubleshooting."""
        self.logger.debug("[%s/%s] Error %d: skipped (targets fields: %s).", target_name, checker, index,
                          ", ".join(targets_fields))

    # ------------------------------------------------------------------ #
    # final agent output (RefactorAgentRun) -- aggregate counts only; the
    # per-error detail (diffs, error text, reasoning) is handled elsewhere
    # in the pipeline and intentionally not logged here.
    # ------------------------------------------------------------------ #
    def log_refactor_result(
            self,
            target_name: str,
            checker: str,
            index: int,
            run: RefactorAgentRun
    ) -> None:
        self._bump(target_name, checker, "refactor_agent_ran")

        if run.error:
            self._bump(target_name, checker, "refactor_errored")
            self.logger.error(
                "[%s/%s] Error %d: RefactorAgent errored.", target_name, checker, index,
            )
        elif run.possible:
            bucket = "refactor_success" if run.success else "refactor_failure"
            sub_bucket = "annotation_only" if run.annotation_only() else "real_changes"
            self._bump(target_name, checker, bucket, sub_bucket)
            self.logger.info(
                "[%s/%s] Error %d: RefactorAgent %s (annotation_only=%s).",
                target_name, checker, index,
                "succeeded" if run.success else "found a fix that failed verification",
                run.annotation_only(),
            )
        else:
            self._bump(target_name, checker, "refactor_not_possible")
            self.logger.info(
                "[%s/%s] Error %d: RefactorAgent determined no fix was possible.",
                target_name, checker, index,
            )

    # ------------------------------------------------------------------ #
    # specimin failures (minimization / compilation / preservation)
    # ------------------------------------------------------------------ #
    def _log_specimin_failure(
            self,
            failure_type: FailureType,
            target_name: str,
            checker: str,
            index: int,
            specimin_cmd: str,
            message: Optional[str],
    ) -> None:
        self._bump(target_name, checker, "specimin_errors", failure_type)
        self.logger.warning(
            "[%s/%s] Error %d: %s failure. Command: %s",
            target_name, checker, index, failure_type, specimin_cmd,
        )
        self._append_block("specimin_failures.log", [
            ("Timestamp", timestamp()),
            ("Target", target_name),
            ("Checker", checker),
            ("ErrorIndex", str(index)),
            ("FailureType", failure_type),
            ("Command", specimin_cmd),
            ("Message", message),
        ])

    def log_failed_minimization(
            self,
            target_name: str,
            checker: str,
            index: int,
            specimin_cmd: str = "",
    ) -> None:
        """Specimin itself crashed / failed to produce minimized output."""
        self._log_specimin_failure("minimization", target_name, checker, index, specimin_cmd, None)

    def log_failed_compilation(
            self,
            target_name: str,
            checker: str,
            index: int,
            specimin_cmd: str = "",
            errors: list[CheckerError] | None = None,
    ) -> None:
        """Minimized output failed to compile."""
        compilation_errors = [e.raw for e in (errors or []) if e.is_compilation_error()]
        message = "\n".join(compilation_errors) if compilation_errors else None
        self._log_specimin_failure("compilation", target_name, checker, index, specimin_cmd, message)

    def log_failed_preservation(
            self,
            target_name: str,
            checker: str,
            index: int,
            specimin_cmd: str = "",
            expected: CheckerError | None = None,
            errors_in_minimized: list[CheckerError] | None = None,
    ) -> None:
        """Minimization succeeded and compiled, but the original error was
        not reproduced in the minimized output."""
        expected_raw = expected.raw if expected is not None else None
        observed_raw = [e.raw for e in (errors_in_minimized or [])]
        message_parts = []
        if expected_raw:
            message_parts.append(f"Expected: {expected_raw}")
        if observed_raw:
            message_parts.append("Observed:\n" + "\n".join(observed_raw))
        message = "\n".join(message_parts) if message_parts else None
        self._log_specimin_failure("preservation", target_name, checker, index, specimin_cmd, message)

    # ------------------------------------------------------------------ #
    # unhandled-exception logging
    # ------------------------------------------------------------------ #
    def log_crash(
            self,
            scope: str,
            target_name: str,
            exc: BaseException,
            checker: str = "",
            index: Optional[int] = None,
    ) -> None:
        """Record an unhandled exception caught by a try/except guard.

        `scope` describes the granularity at which the exception was caught
        and recovered from, e.g. "target", "target_setup", or "error".

        Crashes are NOT counted in summary.json (by design) -- they're
        tracked here in crashes.log only. They ARE still reflected in
        total_errors for error-scoped crashes, via the log_error_start call
        that already ran for that error before processing crashed.
        """
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.logger.error(
            "[%s]%s Unhandled exception at '%s' level: %s: %s",
            target_name,
            f"/{checker}" if checker else "",
            scope,
            type(exc).__name__,
            exc,
        )
        self.logger.debug(tb)

        self._append_block("crashes.log", [
            ("Timestamp", timestamp()),
            ("Scope", scope),
            ("Target", target_name),
            ("Checker", checker),
            ("ErrorIndex", "" if index is None else str(index)),
            ("ExceptionType", type(exc).__name__),
            ("Message", str(exc)),
            ("Traceback", tb),
        ])

    # ------------------------------------------------------------------ #
    # wrap-up
    # ------------------------------------------------------------------ #
    def finalize(self) -> None:
        """Writes summary.json: for every (target, checker) pair seen,
        emits the counts dict described in `_new_target_checker_counts()`
        (setup_method; total_errors; specimin_errors broken down by
        minimization / compilation / preservation; refactor_agent_ran;
        refactor_success and refactor_failure each broken down by
        annotation_only / real_changes; refactor_errored;
        refactor_not_possible)."""
