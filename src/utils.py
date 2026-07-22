import os
import re
import subprocess
from pathlib import Path

from attr import dataclass

_error_parse_regex = re.compile(
    r"(?P<path>.+\.java):(?P<line_num>\d+):\s*(?:error|warning):\s*(?:\[(?P<identifier>[\w.-]+)])?\s*(?P<message>[^\n]*)\n(?:(?!\s*\^\s*\n).*\n)*\s*\^\s*\n(?P<content>(?:(?!.+\.java:\d+:\s*(?:error|warning):|\s*\d+\s+(?:error|warning)s?\s*)[^\n]*\n?)*)",
    re.MULTILINE)

_number_regex = re.compile(r"\d+")

_content_regex = re.compile(r"(?P<prefix>.*):\s*(?P<fields>.*)")


def run_checker_and_parse_errors(checker_cmd: str, cwd: Path) -> list[CheckerError]:
    result = subprocess.run(
        checker_cmd,
        capture_output=True,
        text=True,
        shell=True,
        cwd=cwd
    )
    output = result.stderr
    errors = parse_errors_from_checker_output(output)
    return errors


def parse_errors_from_checker_output(output: str) -> list[CheckerError]:
    errors = []
    matches = _error_parse_regex.finditer(output)
    for match in matches:
        file_path = match.group("path")
        line_number = int(match.group("line_num"))
        identifier = match.group("identifier").strip() if match.group("identifier") else None
        message = match.group("message").strip()
        content = match.group("content").strip()
        raw = match.group().strip()
        errors.append(CheckerError(file_path, line_number, identifier, message, content, raw))
    return errors


def read_file_with_numbered_lines(path: Path) -> str:
    lines = path.read_text().splitlines()
    return "\n".join(f"{i + 1:d}\t{line}" for i, line in enumerate(lines))


def _split_message(message: str) -> tuple[str, set[str] | None]:
    match = _content_regex.match(message)
    if not match:
        return message, None

    return match.group("prefix").strip(), {item.strip() for item in match.group("fields").split(",") if item.strip()}


def run_git_reset_hard(directory: Path):
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=directory)


def run_git_commit(directory: Path):
    subprocess.run(["git", "commit", "-am", "Automated commit"], cwd=directory)


def replace_in_head_commit(search: str, replace: str, repo: Path = Path(".")) -> str:
    """
    Rewrite the current HEAD commit in place: undo it, apply a literal
    find-and-replace to the files it touched, and recommit with the same
    message in its place.

    HISTORY-REWRITING - HEAD gets a new sha. Only use this before pushing,
    or before anyone else has based work on the current HEAD.

    :param search: literal substring to find (not a regex)
    :param replace: literal substring to replace it with
    :param repo: path to the git repository (default: cwd)
    :raises RuntimeError: on a dirty working tree, HEAD being the repo's
        root commit (no parent to reset onto), or if nothing matched and
        empty_ok is False. On any failure, no reset/commit is left
        half-done - either the whole rewrite succeeds or HEAD is restored
        to its original commit before raising.
    """

    def _run(args, check=True):
        return subprocess.run(args, cwd=repo, check=check, text=True, capture_output=True)

    if _run(["git", "status", "--porcelain"]).stdout.strip():
        raise RuntimeError("working tree is not clean; commit or stash first")

    commit_sha = _run(["git", "rev-parse", "HEAD"]).stdout.strip()

    parent = _run(["git", "rev-parse", "HEAD^"], check=False)
    if parent.returncode != 0:
        raise RuntimeError(
            "HEAD has no parent (it's the repo's root commit) - "
            "can't reset onto a parent that doesn't exist"
        )
    parent_sha = parent.stdout.strip()

    changed_files = _run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha]
    ).stdout.splitlines()
    if not changed_files:
        raise RuntimeError(f"{commit_sha} doesn't change any files")

    # Undo the commit but leave its changes staged, ready to be edited
    # and recommitted.
    _run(["git", "reset", "--soft", parent_sha])

    try:
        for rel_path in changed_files:
            full_path = os.path.join(repo, rel_path)
            if not os.path.isfile(full_path):
                continue  # file was deleted by this commit
            with open(full_path, "r", encoding="utf-8", errors="surrogateescape") as f:
                content = f.read()
            if search not in content:
                continue
            with open(full_path, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(content.replace(search, replace))
            _run(["git", "add", "--", rel_path])

        # Reuse the original commit's message (and author/date) verbatim.
        _run(["git", "commit", "-C", commit_sha])
    except Exception:
        # Restore HEAD to exactly where it was before we touched anything,
        # since a half-applied reset is worse than a clean failure.
        _run(["git", "reset", "--hard", commit_sha], check=False)
        raise


@dataclass
class CheckerError:
    file_path: str
    line_number: int
    identifier: str | None
    message: str
    content: str
    raw: str

    def is_compilation_error(self):
        return not self.identifier

    def likely_equals(self, other: "CheckerError") -> bool:
        if Path(self.file_path).name != Path(other.file_path).name or \
                self.identifier != other.identifier:
            return False

        # Sometimes we see an error like this:
        #   the constructor does not initialize fields: converter
        # and then another like this:
        #   the constructor does not initialize fields: converter, some_other_field
        # We want to make sure these are treated as equivalent.

        self_template, self_items = _split_message(self.message)
        other_template, other_items = _split_message(other.message)

        if self_template == other_template and self_items is not None and other_items is not None:
            if self_items.issubset(other_items) or other_items.issubset(self_items):
                return True

        # Sometimes the same error has its content as a subset of the other's, so we check that
        # We also have things like capture#04 which may change to capture#01 in the Specimin output,
        # so we remove digits
        self_lines = {_number_regex.sub("", line.strip()) for line in self.content.splitlines()}
        other_lines = {_number_regex.sub("", line.strip()) for line in other.content.splitlines()}

        return self_lines.issubset(other_lines) or other_lines.issubset(self_lines)
