from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from refactoring.refactor_agent import RefactorAgentRun
from utils import CheckerError

LOGGER_LEVEL = logging.INFO


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class FPMinerLogger:
    """
    Centralized, file-based logger for the fpminer pipeline.

    Creates a per-run directory under `logs/` containing:
      - run.log                     human-readable log of everything (DEBUG+ to file,
                                     INFO+ to console), including full tracebacks
      - failed_minimizations.jsonl  one JSON record per failed minimization
      - failed_compilations.jsonl   one JSON record per failed compilation
      - failed_preservations.jsonl  one JSON record per failed preservation
      - crashes.jsonl               one JSON record per unhandled exception caught
                                     by a try/except guard in the pipeline (per-error
                                     or per-target granularity)
      - refactor_results.jsonl      one JSON record per RefactorAgent run (final
                                     success/possible/error outcome for each error)
      - summary.json                aggregate counts, written by finalize()

    Each *.jsonl file above has a human-readable *.log companion (e.g.
    failed_minimizations.log) with the same content but real line breaks
    instead of escaped `\\n`, for content like commands, raw error text, and
    tracebacks that spans multiple lines.

    All logging calls are defensive: if writing a log record itself fails, that
    failure is swallowed (and reported via the text logger) rather than being
    allowed to crash the pipeline.
    """

    def __init__(self, base_dir: str | Path = "logs", run_id: Optional[str] = None):
        self.run_id = run_id or _timestamp()
        self.run_dir = Path(base_dir) / f"run_{self.run_id}"
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self._counts = {
            "targets_total": 0,
            "targets_crashed": 0,
            "errors_total": 0,
            "skipped": 0,
            "failed_minimizations": 0,
            "failed_compilations": 0,
            "failed_preservations": 0,
            "crashes": 0,
            "succeeded": 0,
            "refactor_runs": 0,
            "refactor_fix_possible": 0,
            "refactor_fix_not_possible": 0,
            "refactor_errored": 0,
        }

        self.logger = self._build_logger()
        self.logger.info("Logging initialized. Run directory: %s", self.run_dir)

    # ------------------------------------------------------------------ #
    # setup
    # ------------------------------------------------------------------ #
    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"fp_miner.{self.run_id}")
        logger.setLevel(LOGGER_LEVEL)
        logger.propagate = False

        # Avoid duplicate handlers if a FailureLogger is somehow constructed twice
        # with the same run_id (e.g. re-entrant calls).
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
    def _append_jsonl(self, filename: str, record: dict[str, Any]) -> None:
        record = {"timestamp": _timestamp(), **record}
        path = self.run_dir / filename
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            # Logging must never be allowed to crash the pipeline.
            self.logger.exception("Failed to write log record to %s", path)

    def _append_text_block(self, filename: str, header: str, sections: list[tuple[str, Optional[str]]]) -> None:
        """Appends a human-readable block to `filename`, companion to a .jsonl
        file of the same name. Unlike JSONL, multi-line fields (commands, raw
        error text, tracebacks) are written with real line breaks instead of
        escaped `\\n`, so the file is pleasant to read/`less`/`grep` directly.

        `sections` is a list of (label, text) pairs; entries with empty/None
        text are skipped.
        """
        path = self.run_dir / filename
        lines = [header, "-" * len(header)]
        for label, text in sections:
            if not text:
                continue
            lines.append(f"{label}:")
            lines.append(text.rstrip("\n"))
            lines.append("")
        lines.append("=" * 80)
        lines.append("")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            self.logger.exception("Failed to write log record to %s", path)

    # ------------------------------------------------------------------ #
    # target / error lifecycle
    # ------------------------------------------------------------------ #
    def start_target(self, target_name: str) -> None:
        self._counts["targets_total"] += 1
        self.logger.info("%s Starting target: %s %s", "=" * 20, target_name, "=" * 20)

    def finish_target(self, target_name: str) -> None:
        self.logger.info("%s Finished target: %s %s", "=" * 20, target_name, "=" * 20)

    def log_error_start(self, target_name: str, checker: str, index: int, total: int) -> None:
        self._counts["errors_total"] += 1
        self.logger.info("[%s/%s] Processing error %d/%d", target_name, checker, index, total)

    def log_success(self, target_name: str, checker: str, index: int) -> None:
        self._counts["succeeded"] += 1
        self.logger.info("[%s/%s] Error %d: processed successfully.", target_name, checker, index)

    def log_skipped_error(self, target_name: str, targets_fields: list[str], checker: str, index: int):
        self._counts["skipped"] += 1
        self.logger.info("[%s/%s] Error %d: skipped (targets fields: %s).", target_name, checker, index,
                         ", ".join(targets_fields))

    # ------------------------------------------------------------------ #
    # final agent output (RefactorAgentRun)
    # ------------------------------------------------------------------ #
    def log_refactor_result(
            self,
            target_name: str,
            checker: str,
            index: int,
            run: RefactorAgentRun,
            specimin_cmd: str = "",
    ) -> None:
        """Records the final output of RefactorAgent.run(error) for a given
        error — i.e. a `RefactorAgentRun`-shaped object with `success`,
        `possible`, `error`, and `not_possible_reason` fields.

        Written to:
          - refactor_results.jsonl  one record per error (machine-readable)
          - refactor_results.log    human-readable companion
        """
        success = run.success
        possible = run.possible
        error_msg = run.error
        not_possible_reason = run.not_possible_reason

        self._counts["refactor_runs"] += 1
        if error_msg:
            self._counts["refactor_errored"] += 1
        elif possible:
            self._counts["refactor_fix_possible"] += 1
        elif not possible:
            self._counts["refactor_fix_not_possible"] += 1

        if error_msg:
            self.logger.error(
                "[%s/%s] Error %d: RefactorAgent errored: %s", target_name, checker, index, error_msg,
            )
        elif possible:
            self.logger.info(
                "[%s/%s] Error %d: RefactorAgent found a possible fix (success=%s).",
                target_name, checker, index, success,
            )
        else:
            self.logger.info(
                "[%s/%s] Error %d: RefactorAgent determined no fix was possible. %s",
                target_name, checker, index, not_possible_reason or "",
            )

        self._append_jsonl("refactor_results.jsonl", {
            "target": target_name,
            "checker": checker,
            "error_index": index,
            "specimin_cmd": specimin_cmd,
            "success": success,
            "possible": possible,
            "error": error_msg,
            "not_possible_reason": not_possible_reason,
        })
        self._append_text_block(
            "refactor_results.log",
            header=(
                f"[{_timestamp()}] {target_name}/{checker} — error {index} — "
                f"REFACTOR RESULT (success={success}, possible={possible})"
            ),
            sections=[
                ("Command", specimin_cmd),
                ("Error", error_msg),
                ("Not-possible reason", not_possible_reason),
            ],
        )

    # ------------------------------------------------------------------ #
    # expected-failure logging (not exceptions, just pipeline outcomes)
    # ------------------------------------------------------------------ #
    def log_failed_minimization(
            self,
            target_name: str,
            checker: str,
            index: int,
            specimin_cmd: str = "",
    ) -> None:
        """Mirrors the old `add_to_failed_minimizations`: records the exact
        Specimin invocation (the `./gradlew run --args=...` command) that
        failed to minimize the error."""
        self._counts["failed_minimizations"] += 1
        self.logger.warning("[%s/%s] Error %d: minimization failed. Command: %s",
                            target_name, checker, index, specimin_cmd)
        self._append_jsonl("failed_minimizations.jsonl", {
            "target": target_name,
            "checker": checker,
            "error_index": index,
            "specimin_cmd": specimin_cmd,
        })
        self._append_text_block(
            "failed_minimizations.log",
            header=f"[{_timestamp()}] {target_name}/{checker} — error {index} — FAILED MINIMIZATION",
            sections=[("Command", specimin_cmd)],
        )

    def log_failed_compilation(
            self,
            target_name: str,
            checker: str,
            index: int,
            specimin_cmd: str = "",
            errors: list[CheckerError] | None = None,
    ) -> None:
        """Mirrors the old `add_to_failed_compilations`: records the Specimin
        command plus the raw text of every compilation error found in the
        minimized output."""
        self._counts["failed_compilations"] += 1
        compilation_errors = [
            e.raw for e in (errors or []) if e.is_compilation_error()
        ]
        self.logger.warning("[%s/%s] Error %d: minimized output failed to compile. Command: %s",
                            target_name, checker, index, specimin_cmd)
        self._append_jsonl("failed_compilations.jsonl", {
            "target": target_name,
            "checker": checker,
            "error_index": index,
            "specimin_cmd": specimin_cmd,
            "errors": compilation_errors,
        })
        self._append_text_block(
            "failed_compilations.log",
            header=f"[{_timestamp()}] {target_name}/{checker} — error {index} — FAILED COMPILATION",
            sections=[
                ("Command", specimin_cmd),
                ("Compilation errors", "\n".join(compilation_errors) if compilation_errors else None),
            ],
        )

    def log_failed_preservation(
            self,
            target_name: str,
            checker: str,
            index: int,
            specimin_cmd: str = "",
            expected: CheckerError | None = None,
            errors_in_minimized: list[CheckerError] | None = None,
    ) -> None:
        """Mirrors the old `add_to_failed_preservations`: records the Specimin
        command, the raw text of the expected error, and the raw text of every
        error actually observed in the minimized output."""
        self._counts["failed_preservations"] += 1
        expected_raw = expected.raw if expected is not None else None
        observed_raw = [e.raw for e in (errors_in_minimized or [])]
        self.logger.warning(
            "[%s/%s] Error %d: minimization failed to preserve the original error. Command: %s",
            target_name, checker, index, specimin_cmd,
        )
        self._append_jsonl("failed_preservations.jsonl", {
            "target": target_name,
            "checker": checker,
            "error_index": index,
            "specimin_cmd": specimin_cmd,
            "expected": expected_raw,
            "errors_in_minimized": observed_raw,
        })
        self._append_text_block(
            "failed_preservations.log",
            header=f"[{_timestamp()}] {target_name}/{checker} — error {index} — FAILED PRESERVATION",
            sections=[
                ("Command", specimin_cmd),
                ("Expected", expected_raw),
                ("Errors in minimized output", "\n".join(observed_raw) if observed_raw else None),
            ],
        )

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

        `scope` describes the granularity at which the exception was caught and
        recovered from, e.g. "target", "target_setup", or "error".
        """
        self._counts["crashes"] += 1
        if scope in ("target", "target_setup"):
            self._counts["targets_crashed"] += 1

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

        self._append_jsonl("crashes.jsonl", {
            "scope": scope,
            "target": target_name,
            "checker": checker,
            "error_index": index,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": tb,
        })
        self._append_text_block(
            "crashes.log",
            header=(
                f"[{_timestamp()}] {target_name}"
                f"{f'/{checker}' if checker else ''} — {f'error {index} — ' if index is not None else ''}"
                f"CRASH at '{scope}' level: {type(exc).__name__}"
            ),
            sections=[
                ("Message", str(exc)),
                ("Traceback", tb),
            ],
        )

    # ------------------------------------------------------------------ #
    # wrap-up
    # ------------------------------------------------------------------ #
    def finalize(self) -> None:
        summary_path = self.run_dir / "summary.json"
        try:
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(self._counts, f, indent=2)
        except Exception:
            self.logger.exception("Failed to write summary.json")
        self.logger.info("Run complete. Summary: %s", self._counts)
