import json
import threading
from pathlib import Path

from java.annotation_compare import are_changes_annotation_or_comment_only
from refactoring.refactor_agent import RefactorAgentRun

RESULT_DIR = Path("result")
SUCCESS_DIR = RESULT_DIR / "success"
FAILURE_DIR = RESULT_DIR / "failure"

FALSE_POSITIVES_SUCCESS = SUCCESS_DIR / "false_positives.jsonl"
ANNOTATION_ONLY_SUCCESS = SUCCESS_DIR / "annotation.jsonl"
FALSE_POSITIVES_FAILURE = FAILURE_DIR / "false_positives.jsonl"
ANNOTATION_ONLY_FAILURE = FAILURE_DIR / "annotation.jsonl"

SUCCESS_DIR.mkdir(parents=True, exist_ok=True)
FAILURE_DIR.mkdir(parents=True, exist_ok=True)

_false_positives_success_lock = threading.Lock()
_annotation_only_success_lock = threading.Lock()
_false_positives_failure_lock = threading.Lock()
_annotation_only_failure_lock = threading.Lock()


def _append_record(path: Path, record: dict, lock: threading.Lock) -> None:
    line = json.dumps(record) + "\n"
    with lock:
        with path.open("a") as f:
            f.write(line)


def handle_refactor_result(result: RefactorAgentRun):
    record = {"original": result.orig_content, "modified": result.modified_content}

    if are_changes_annotation_or_comment_only(result.orig_content, result.modified_content):
        _append_record(ANNOTATION_ONLY_SUCCESS if result.success else ANNOTATION_ONLY_FAILURE, record,
                       _annotation_only_success_lock if result.success else _annotation_only_failure_lock)
    else:
        _append_record(FALSE_POSITIVES_SUCCESS if result.success else FALSE_POSITIVES_FAILURE, record,
                       _false_positives_success_lock if result.success else _false_positives_failure_lock)
