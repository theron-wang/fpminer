import json
import os
import threading
from pathlib import Path

from differential_tester import DifferentialTestResult
from java.annotation_compare import are_changes_annotation_or_comment_only
from refactoring.refactor_agent import RefactorAgentRun


def _write(path: Path, result: RefactorAgentRun, lock: threading.Lock):
    metadata = {"success": result.success, "possible": result.possible,
                "semantic_equivalence": result.differential_test_result.name, "error": result.error}

    with lock:
        with path.open("a") as f:
            f.write("==================>\n")
            f.write(f"{json.dumps(metadata)}\n")
            f.write("== ORIGINAL =======\n")
            f.write(result.orig_content + "\n")
            f.write("== MODIFIED =======\n")
            f.write(result.modified_content + "\n")
            f.write("<==================\n\n")


class RefactorResultHandler:
    def __init__(self, run_id: str, target_name: str, checker: str):
        self.run_id = run_id
        self.target_name = target_name
        self.checker = checker

        self.false_positives_success = Path(
            "results") / f"run_{run_id}" / "output" / "success" / "false_positives.txt"
        self.annotations_success = Path("results") / f"run_{run_id}" / "output" / "success" / "annotation_only.txt"
        self.other_failures = Path(
            "results") / f"run_{run_id}" / "output" / "failure" / "other.txt"
        self.annotations_failure = Path("results") / f"run_{run_id}" / "output" / "failure" / "annotation_only.txt"
        self.inconclusive = Path("results") / f"run_{run_id}" / "output" / "inconclusive" / "inconclusive.txt"

        os.makedirs(self.false_positives_success.parent, exist_ok=True)
        os.makedirs(self.other_failures.parent, exist_ok=True)
        os.makedirs(self.inconclusive.parent, exist_ok=True)

        self.false_positives_success_lock = threading.Lock()
        self.annotations_success_lock = threading.Lock()
        self.other_failures_lock = threading.Lock()
        self.annotations_failure_lock = threading.Lock()
        self.inconclusive_lock = threading.Lock()

    def handle_refactor_result(self, result: RefactorAgentRun):
        if are_changes_annotation_or_comment_only(result.orig_content, result.modified_content):
            _write(self.annotations_success if result.success else self.annotations_failure, result,
                   self.annotations_success_lock if result.success else self.annotations_failure_lock)
        elif result.differential_test_result == DifferentialTestResult.INCONCLUSIVE:
            _write(self.inconclusive, result, self.inconclusive_lock)
        elif result.success:
            _write(self.false_positives_success, result, self.false_positives_success_lock)
        else:
            _write(self.other_failures, result, self.other_failures_lock)
