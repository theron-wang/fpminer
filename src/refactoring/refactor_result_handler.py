import filecmp
import json
import subprocess
from pathlib import Path

from refactor_agent import RefactorAgentRun

REFACTOR_PAIRINGS_LOG = Path("refactor_pairings.jsonl")


def handle_refactor_result(result: RefactorAgentRun):
    if not result.success:
        return

    original_lines, modified_lines = _find_and_diff_changed_file(result.orig_dir, result.modified_dir)
    _save_pairing(original_lines, modified_lines)


def _find_and_diff_changed_file(orig_dir: Path, modified_dir: Path) -> tuple[str, str]:
    """Trees are assumed identical in structure. Find the single file whose
    contents differ and return (original_lines, modified_lines) from unix diff."""
    changed_file = None

    for orig_file in orig_dir.rglob("*"):
        if orig_file.is_dir():
            continue
        rel = orig_file.relative_to(orig_dir)
        modified_file = modified_dir / rel

        if not filecmp.cmp(orig_file, modified_file, shallow=False):
            changed_file = (orig_file, modified_file)
            break  # trees are identical, so there's exactly one — stop here

    if changed_file is None:
        raise ValueError(
            f"Expected exactly one differing file between {orig_dir} "
            f"and {modified_dir}, found none."
        )

    orig_file, modified_file = changed_file
    proc = subprocess.run(
        ["diff", str(orig_file), str(modified_file)],
        capture_output=True,
        text=True,
    )
    if proc.returncode > 1:
        raise RuntimeError(f"diff failed: {proc.stderr}")

    return _split_diff(proc.stdout)


def _split_diff(diff_output: str) -> tuple[str, str]:
    """Split unix diff output into the '<' (original) and '>' (modified) sides,
    stripping the leading markers/headers."""
    original_lines = []
    modified_lines = []

    for line in diff_output.splitlines():
        if line.startswith("< "):
            original_lines.append(line[2:])
        elif line.startswith("> "):
            modified_lines.append(line[2:])
        # lines like "3c3", "---", "5a6" etc. are separators/headers — skipped

    return "\n".join(original_lines), "\n".join(modified_lines)


def _save_pairing(original_lines: str, modified_lines: str) -> None:
    record = {"original": original_lines, "modified": modified_lines}
    with REFACTOR_PAIRINGS_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
