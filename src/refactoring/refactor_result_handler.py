import json
import os
import threading
from pathlib import Path

from differential_tester import DifferentialTestResult
from refactoring.refactor_agent import RefactorAgentRun


def _write(path: Path, result: RefactorAgentRun, error_num: int, lock: threading.Lock):
    metadata = {"success": result.success, "possible": result.possible,
                "semantic_equivalence": result.differential_test_result.name, "error": result.error}

    with lock:
        with path.open("a") as f:
            f.write(f"==================> Error {error_num}\n")
            f.write(str(result.source_dir.resolve()) + "\n")
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

        base_path = Path("results") / f"run_{run_id}" / "output" / target_name / checker

        self.false_positives_success = base_path / "success" / "false_positives.txt"
        self.annotations_success = base_path / "success" / "annotation_only.txt"
        self.other_failures = base_path / "failure" / "other.txt"
        self.annotations_failure = base_path / "failure" / "annotation_only.txt"
        self.inconclusive = base_path / "unresolved" / "inconclusive.txt"
        self.not_possible = base_path / "unresolved" / "not_possible.txt"

        os.makedirs(self.false_positives_success.parent, exist_ok=True)
        os.makedirs(self.other_failures.parent, exist_ok=True)
        os.makedirs(self.inconclusive.parent, exist_ok=True)

        self.false_positives_success_lock = threading.Lock()
        self.annotations_success_lock = threading.Lock()
        self.other_failures_lock = threading.Lock()
        self.annotations_failure_lock = threading.Lock()
        self.inconclusive_lock = threading.Lock()
        self.not_possible_lock = threading.Lock()

    def handle_refactor_result(self, result: RefactorAgentRun, error_num: int):
        if result.annotation_only():
            _write(self.annotations_success if result.success else self.annotations_failure, result, error_num,
                   self.annotations_success_lock if result.success else self.annotations_failure_lock)
        elif result.success:
            _write(self.false_positives_success, result, error_num, self.false_positives_success_lock)
        elif not result.error and result.differential_test_result == DifferentialTestResult.INCONCLUSIVE:
            _write(self.inconclusive, result, error_num, self.inconclusive_lock)
        elif result.error and not result.possible:
            _write(self.not_possible, result, error_num, self.not_possible_lock)
        else:
            _write(self.other_failures, result, error_num, self.other_failures_lock)
