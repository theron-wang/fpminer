import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import IO, cast

import docker
import tree_sitter_bash
from analysis_agent.mini_orchestrator import sanitize_for_filename
from analysis_agent.replay_producer import produce_replay
from tree_sitter import Language, Parser
from utils import CheckerError, parse_errors_from_checker_output, ensure_unbounded_diagnostics_and_cf_only_errors, \
    GRADLE_AUGMENT_SCRIPT_PATH, GRADLE_AUGMENT_SCRIPT_NAME

ANALYSIS_AGENT_ROOT = Path("analysis_agent")
ANALYSIS_AGENT_LOGS = ANALYSIS_AGENT_ROOT / "logs"
REPLAY_CWD_REGEX = re.compile(r"log_info 'Working directory: (?P<cwd>.*)'")
JAVA_FILE_ARG_REGEX = re.compile(r'(?<!\S)(?P<path>[^\s"\']+\.java)(?!\S)')


def run_analysis_agent(target_name: str, target_url: str, tool_name: str, tool_url: str) -> \
        (list[CheckerError] | None):
    # Try reconstructing first; if it fails, it's probably a stale directory from
    # a failed earlier attempt, and we'll try again.
    try:
        checker_output = _reconstruct(target_name, tool_name, f"targets/{target_name}")

        return parse_errors_from_checker_output(checker_output)
    except KeyboardInterrupt:
        raise
    except Exception:
        pass

    os.makedirs(ANALYSIS_AGENT_ROOT, exist_ok=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "analysis_agent.main",
            "--tool-name", tool_name,
            "--tool-url", tool_url,
            "--target-name", target_name,
            "--target-url", target_url,
            "--model", os.environ["EXEC_AGENT_MODEL"],
            "--max-attempts", "3",
            "--cycle-budget", "40",
            "--time-limit-seconds", "10800",
            "--disable-exit-attempt",
            # In case you encounter errors with AnalysisAgent being unable to start new containers, you can
            # uncomment the following line for it to run successfully. Note that this will break the replay
            # because it will not generate launch.sh.
            # "--docker-image", "ubuntu:22.04"
        ],
        cwd=ANALYSIS_AGENT_ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    if result.returncode != 0:
        return None

    # AnalysisAgent automatically cleans up the Docker container after execution,
    # so we need to reconstruct the output
    checker_output = _reconstruct(target_name, tool_name, f"targets/{target_name}")

    return parse_errors_from_checker_output(checker_output)


def _get_target_directory(target_name: str) -> Path:
    return Path("targets") / target_name


def _find_target_command_range(
        source: bytes,
        base_offset: int,
        tool_name: str,
        bash_lang: "Language",
) -> tuple[int, int] | None:
    """
    Parse `source` as a bash script and find the last `command` node whose text contains
    `tool_name`, descending into heredoc bodies (which tree-sitter-bash treats as opaque
    text rather than parsing structurally, since a heredoc's target may not even be a
    shell) by reparsing them as nested scripts.

    Byte offsets in the return value are relative to the ORIGINAL top-level source, via
    `base_offset` — this stays correct because a heredoc body's bytes are copied verbatim
    from the source file (no escaping/expansion changes the byte content itself).
    """
    parser = Parser(bash_lang)
    tree = parser.parse(source)

    matches: list[tuple[int, int]] = []

    def visit(node):
        if node.type == "command":
            if tool_name in node.text.decode("utf-8"):
                matches.append((base_offset + node.start_byte, base_offset + node.end_byte))
        elif node.type == "heredoc_body":
            nested = _find_target_command_range(
                source[node.start_byte:node.end_byte],
                base_offset + node.start_byte,
                tool_name,
                bash_lang,
            )
            if nested is not None:
                matches.append(nested)
            return  # heredoc_body has no shell-structural children of its own to visit
        for child in node.children:
            visit(child)

    visit(tree.root_node)

    if not matches:
        return None

    # Sort by position: append order from the DFS above roughly tracks document order for
    # siblings, but a parent "command" node (e.g. one containing a $(...) substitution) gets
    # appended before its own nested substitution's "command" node even though the
    # substitution's bytes are a subset located earlier — sorting makes "last in the file"
    # unambiguous regardless of traversal quirks.
    matches.sort(key=lambda m: m[0])
    return matches[-1]


def _generalize_single_java_file_target(checker_cmd: str) -> str:
    """
    If `checker_cmd` targets exactly one hard-coded `.java` file (as opposed to a command
    that already covers a tree via -sourcepath/find/glob), rewrite it to instead process
    every `.java` file under that file's directory. This avoids under-reporting false
    positives that only manifest when compiling the target's full source tree together.
    AnalysisAgent _may_ sometimes give output where only one or a few files are mentioned,
    but we should run the analysis on the ENTIRE project.

    Leaves the command unchanged if:
    - it references zero or more than one `.java` file explicitly (ambiguous to rewrite
      safely, or already covers multiple files), or
    - it invokes gradlew/mvnw, which already build the whole module/project regardless
      of any single file mentioned on the command line.
    """
    if "gradlew" in checker_cmd or "mvnw" in checker_cmd:
        return checker_cmd

    matches = list(JAVA_FILE_ARG_REGEX.finditer(checker_cmd))
    # Use 10 as a heuristic threshold for "more than one" because some commands may have a few (like 2 or 3)
    if not matches or len(matches) > 10:
        return checker_cmd

    match = matches[0]

    find_all = "$(find . -name '*.java')"
    return checker_cmd[:match.start()] + find_all + checker_cmd[match.end():]


def _instrument_replay_for_output(path_to_replay_sh: Path, orig_tool_name: str, new_tool_name: str,
                                  output_path: str) -> Path:
    """
    Rewrite replay.sh so that the command running the annotation processor for `tool_name`
    (a) has unbounded-diagnostics args added via ensure_unbounded_diagnostics_and_cf_only_errors,
    and (b) has its stderr redirected to `output_path` (a path inside the container).

    Handles the tool's command living inside a heredoc (e.g. `bash <<'EOF' ... EOF`), not
    just at the top level of replay.sh.

    Writes the rewritten script alongside the original and returns the path to the new file.
    """
    bash_lang = Language(tree_sitter_bash.language())

    with open(path_to_replay_sh, 'rb') as f:
        source = f.read()

    target_range = _find_target_command_range(source, 0, orig_tool_name, bash_lang)

    if target_range is None:
        raise RuntimeError(f"Could not find a command referencing {orig_tool_name} in {path_to_replay_sh}")

    target_start, target_end = target_range

    original_command = source[target_start:target_end].decode("utf-8")
    generalized_command = _generalize_single_java_file_target(original_command)
    augmented_command = ensure_unbounded_diagnostics_and_cf_only_errors(generalized_command)
    replacement = f"{augmented_command} 2> {output_path}".encode("utf-8")

    instrumented_source = source[:target_start] + replacement + source[target_end:]
    instrumented_source = instrumented_source.replace(orig_tool_name.encode("utf-8"), new_tool_name.encode("utf-8"))

    instrumented_path = path_to_replay_sh.with_name(
        f"{path_to_replay_sh.stem}.instrumented{path_to_replay_sh.suffix}"
    )
    with open(instrumented_path, "wb") as f:
        f.write(instrumented_source)

    return instrumented_path


def _reconstruct(target_name: str, tool_name: str, output_dir: str) -> str:
    extracted_errors_filename = "fpminer-extracted-errors.txt"

    # logs directory contains paths like this:
    # {tool name}_{target name}_{timestamp}

    target_dir_section = f"_{sanitize_for_filename(target_name)}_"

    # We will try to find the max timestamp to get the most recent run
    most_recent_log_path = max(
        (f for f in os.listdir(ANALYSIS_AGENT_LOGS) if target_dir_section in f),
        key=lambda f: int(f.rsplit("_", 1)[-1])
    )

    if not most_recent_log_path:
        raise RuntimeError(f"No logs found for target {target_name}")

    orig_checker = most_recent_log_path.split('_')[0]

    most_recent_log_path = ANALYSIS_AGENT_LOGS / most_recent_log_path

    # All paths will be attempt_#/; get the last attempt
    last_attempt = max(f for f in os.listdir(most_recent_log_path) if f.startswith("attempt_"))
    most_recent_log_path /= last_attempt

    replay_output_dir = Path("analysis_agent/replay") / tool_name / target_name

    # It seems that AnalysisAgent's implementation of attempt_number is incorrect, as we have to
    # pass in the attempt_# directory of the output anyway. So, we set it to 1, so we always have
    # the replay put in attempt_1/, and then we can find the replay.sh there.
    success = produce_replay(
        log_dir=most_recent_log_path,
        output_dir=replay_output_dir,
        attempt_number=1,
        tool_name=orig_checker,
        target_name=target_name,
        require_successful_docker=False
    )

    if not success:
        raise RuntimeError(f"Failed to produce replay for target {target_name} and tool {tool_name}")

    # Since we pass in attempt_number=1 above, the replay.sh will be in attempt_1/
    replay_dir = replay_output_dir / "attempt_1"
    replay_sh_path = Path(replay_dir).absolute() / "replay.sh"

    working_dir = REPLAY_CWD_REGEX.search(replay_sh_path.read_text()).group("cwd")
    working_dir = Path(working_dir)

    # Write into /app so it gets pulled out along with the rest of the archive below
    extracted_errors_output_path = working_dir / extracted_errors_filename
    instrumented_replay_sh = _instrument_replay_for_output(replay_sh_path, orig_checker, tool_name,
                                                           str(extracted_errors_output_path))

    result = subprocess.run(["./launch.sh", "--build"], capture_output=True, text=True, check=True, cwd=replay_dir)

    match = re.search(r"^Image name: (\S+)$", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError("Could not find image name in launch.sh output")

    image_name = match.group(1)

    client = docker.from_env()

    shutil.rmtree(output_dir, ignore_errors=True)

    os.makedirs(output_dir, exist_ok=True)

    container = client.containers.run(
        image=image_name,
        detach=True,
        entrypoint=["/bin/bash", "/replay.sh"],
        volumes={
            str(instrumented_replay_sh): {
                "bind": "/replay.sh",
                "mode": "ro"
            },
            str(GRADLE_AUGMENT_SCRIPT_PATH.resolve()): {
                "bind": "/" + GRADLE_AUGMENT_SCRIPT_NAME,
                "mode": "ro"
            }
        }
    )

    try:
        container.wait()
        bits, _ = container.get_archive(working_dir)

        tar_stream = io.BytesIO()
        for chunk in bits:
            tar_stream.write(chunk)
        tar_stream.seek(0)

        with tarfile.open(fileobj=cast(IO[bytes], tar_stream)) as tar:
            tar.extractall(path=output_dir, filter="data")
    finally:
        container.remove(force=True)
        client.images.remove(image_name, force=True)

    extracted_errors_filepath = Path(output_dir) / str(working_dir).strip('/') / extracted_errors_filename
    if not extracted_errors_filepath.exists():
        raise RuntimeError(
            f"Expected {extracted_errors_filename} was not found in extracted output at {extracted_errors_filepath}"
        )

    replace_regex = re.compile(f"^/{str(working_dir).strip('/')}/")
    replace_with = f"{str(Path(output_dir).absolute())}/"

    return replace_regex.sub(replace_with, extracted_errors_filepath.read_text())
