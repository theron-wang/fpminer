import io
import os
import re
import subprocess
import tarfile
from pathlib import Path
from typing import IO, cast

import docker
import tree_sitter_bash
from analysis_agent.mini_orchestrator import sanitize_for_filename, run_with_attempts
from analysis_agent.replay_producer import produce_replay
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_model import LitellmModel
from tree_sitter import Language, Parser
from utils import CheckerError, parse_errors_from_checker_output

LOGS_ROOT = Path("analysis_agent_logs")


def run_analysis_agent(target_name: str, target_url: str, tool_name: str, tool_url: str) -> list[CheckerError]:
    success = True

    print(f"Running AnalysisAgent on {target_name} with tool {tool_name}")

    if os.path.exists(_get_target_directory(target_name)):
        print(f"Target {target_name} already exists, skipping")
    else:
        model = LitellmModel(model_name=os.environ["EXEC_AGENT_MODEL"])
        env = LocalEnvironment(
            cwd="analysis_agent_workspace",
        )

        success, message = run_with_attempts(
            model=model,
            env=env,
            tool_name=tool_name,
            tool_url=tool_url,
            target_name=target_name,
            target_url=target_url,
            max_attempts=3,
            cycle_budget=40,
            mode="auto",
            time_limit_seconds=10800,
            enable_exit_attempt=False,
            logs_root=LOGS_ROOT
        )

    if not success:
        return None

    # AnalysisAgent automatically cleans up the Docker container after execution,
    # so we need to reconstruct the output
    print(f"Reconstructing output for target {target_name} with tool {tool_name}")
    checker_output = _reconstruct(target_name, tool_name, f"targets/{target_name}")
    print("Reconstruction complete.")
    print()

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
    shell) by re-parsing them as nested scripts.

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


def _instrument_replay_for_output(path_to_replay_sh: Path, tool_name: str, output_path: str) -> Path:
    """
    Rewrite replay.sh so that the command running the annotation processor for `tool_name`
    also redirects its stderr to `output_path` (a path inside the container).

    Handles the tool's command living inside a heredoc (e.g. `bash <<'EOF' ... EOF`), not
    just at the top level of replay.sh.

    Writes the rewritten script alongside the original and returns the path to the new file.
    """
    bash_lang = Language(tree_sitter_bash.language())

    with open(path_to_replay_sh, 'rb') as f:
        source = f.read()

    target_range = _find_target_command_range(source, 0, tool_name, bash_lang)

    if target_range is None:
        raise RuntimeError(f"Could not find a command referencing {tool_name} in {path_to_replay_sh}")

    _, target_end = target_range

    # Insert a stderr redirect right after the matched command, wherever it lives (including
    # inside a heredoc), without changing what the command otherwise does.
    redirect = f" 2> {output_path}".encode("utf-8")
    instrumented_source = source[:target_end] + redirect + source[target_end:]

    instrumented_path = path_to_replay_sh.with_name(
        f"{path_to_replay_sh.stem}.instrumented{path_to_replay_sh.suffix}"
    )
    with open(instrumented_path, "wb") as f:
        f.write(instrumented_source)

    return instrumented_path


def _reconstruct(target_name: str, tool_name: str, output_dir: str) -> str:
    classpath_filename = "fp-miner-classpath.txt"

    existing_classpath_file = _get_target_directory(target_name) / classpath_filename
    if os.path.exists(existing_classpath_file):
        with open(existing_classpath_file, "r") as f:
            return f.read()

    prefix = f"{sanitize_for_filename(tool_name)}_{sanitize_for_filename(target_name)}"

    # logs directory contains paths like this:
    # {tool name}_{target name}_{timestamp}

    # We will try to find the max timestamp to get the most recent run
    most_recent_log_path = max(f for f in os.listdir(LOGS_ROOT) if f.startswith(prefix))

    if not most_recent_log_path:
        raise RuntimeError(f"No logs found for target {target_name} and tool {tool_name}")

    most_recent_log_path = LOGS_ROOT / most_recent_log_path

    # All paths will be attempt_#/; get the last attempt
    last_attempt = max(f for f in os.listdir(most_recent_log_path) if f.startswith("attempt_"))
    most_recent_log_path /= last_attempt

    success = True

    if not os.path.exists(_get_target_directory(target_name)):
        success = produce_replay(
            log_dir=most_recent_log_path,
            output_dir=Path("replay"),
            attempt_number=1,
            tool_name=tool_name,
            target_name=target_name,
            require_successful_docker=True
        )

    if not success:
        raise RuntimeError(f"Failed to produce replay for target {target_name} and tool {tool_name}")

    replay_dir = Path("replay") / last_attempt
    replay_sh_path = Path(replay_dir).absolute() / "replay.sh"

    # Write into /app so it gets pulled out along with the rest of the archive below
    classpath_output_path = f"/app/{classpath_filename}"
    instrumented_replay_sh = _instrument_replay_for_output(replay_sh_path, tool_name, classpath_output_path)

    result = subprocess.run(["./launch.sh", "--build"], capture_output=True, text=True, check=True, cwd=replay_dir)

    match = re.search(r"^Image name: (\S+)$", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError("Could not find image name in launch.sh output")

    image_name = match.group(1)

    client = docker.from_env()
    os.makedirs(output_dir, exist_ok=True)

    container = client.containers.run(
        image=image_name,
        detach=True,
        entrypoint=["/bin/bash", "/replay.sh"],
        volumes={
            str(instrumented_replay_sh): {
                "bind": "/replay.sh",
                "mode": "ro",
            }
        }
    )

    container.wait()

    try:
        bits, _ = container.get_archive("/app")

        tar_stream = io.BytesIO()
        for chunk in bits:
            tar_stream.write(chunk)
        tar_stream.seek(0)

        with tarfile.open(fileobj=cast(IO[bytes], tar_stream)) as tar:
            tar.extractall(path=output_dir)
    finally:
        container.remove(force=True)
        client.images.remove(image_name, force=True)

    extracted_classpath_path = Path(output_dir) / "app" / classpath_filename
    if not extracted_classpath_path.exists():
        raise RuntimeError(
            f"Expected {classpath_filename} was not found in extracted output at {extracted_classpath_path}"
        )

    return extracted_classpath_path.read_text()
