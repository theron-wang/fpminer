import os
import re
import subprocess
from collections import deque
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


def replace_in_uncommitted_changes(search: str, replace: str, repo: Path = Path(".")):
    """
    Apply a literal find-and-replace to every file with uncommitted changes
    (staged or unstaged) relative to HEAD.

    Only touches the working tree - nothing is committed, and no history
    is rewritten. Safe to use at any point, including on shared/pushed
    branches.

    :param search: literal substring to find (not a regex)
    :param replace: literal substring to replace it with
    :param repo: path to the git repository (default: cwd)
    """

    def _run(args, check=True):
        return subprocess.run(args, cwd=repo, check=check, text=True, capture_output=True)

    changed_files = _run(
        ["git", "diff", "--name-only", "HEAD"]
    ).stdout.splitlines()

    for rel_path in changed_files:
        full_path = os.path.join(repo, rel_path)
        if not os.path.isfile(full_path):
            continue  # file was deleted in the uncommitted changes
        with open(full_path, "r", encoding="utf-8", errors="surrogateescape") as f:
            content = f.read()
        if search not in content:
            continue
        with open(full_path, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(content.replace(search, replace))


def find_build_system_file(base_dir: Path) -> Path | None:
    """
    Detects the build system file in a folder. Breadth-first search for
    either build.gradle(.kts) or pom.xml, and returns None if
    neither was found.
    :param base_dir: The base directory
    :return: "maven", "gradle", or None
    """
    queue = deque[Path]([base_dir])
    ignored = {".git", "target", "build", ".gradle"}

    while queue:
        current = queue.popleft()

        for path in current.iterdir():
            if path.is_file() and path.name in ("pom.xml", "build.gradle", "build.gradle.kts"):
                return path
            elif path.is_dir() and path.name not in ignored:
                queue.append(path)

    return None


def find_source_dir(path_to_full_project: Path, rel_path: Path) -> Path | None:
    """
    BFS under path_to_full_project for the first directory `d` such that
    d / rel_path exists as a file. Returns that directory
    (the "source directory"), or None if not found.
    """
    queue = deque([path_to_full_project])

    while queue:
        current_dir = queue.popleft()

        candidate = current_dir / rel_path
        if os.path.isfile(candidate):
            return current_dir

        try:
            entries = sorted(os.listdir(current_dir))
        except (PermissionError, FileNotFoundError):
            continue

        for entry in entries:
            full_path = current_dir / entry
            if os.path.isdir(full_path):
                queue.append(full_path)

    return None


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
