import re
import subprocess
from pathlib import Path

from attr import dataclass

_error_parse_regex = re.compile(
    r"(?P<path>.+\.java):(?P<line_num>\d+):\s*(?:error|warning):\s*(?:\[(?P<identifier>[\w.-]+)])?\s*(?P<message>[^\n]*)\n(?:(?!\s*\^\s*\n).*\n)*\s*\^\s*\n(?P<content>(?:(?!.+\.java:\d+:\s*(?:error|warning):|\s*\d+\s+(?:error|warning)s?\s*)[^\n]*\n?)*)",
    re.MULTILINE)

_number_regex = re.compile(r"\d+")


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

    def likely_equals(self, other: CheckerError) -> bool:
        if Path(self.file_path).name != Path(other.file_path).name or \
                self.identifier != other.identifier or self.message != other.message:
            return False

        # Sometimes the same error has its content as a subset of the other's, so we check that
        # We also have things like capture#04 which may change to capture#01 in the Specimin output,
        # so we remove digits
        self_lines = {_number_regex.sub("", line.strip()) for line in self.content.splitlines()}
        other_lines = {_number_regex.sub("", line.strip()) for line in other.content.splitlines()}

        return self_lines.issubset(other_lines) or other_lines.issubset(self_lines)
