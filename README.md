# false-positives-miner

Pipeline for mining and reducing Checker Framework false positives across target Java repositories.

## Setup

```bash
uv sync
```

Create a `.env` file in the repository root:

```dotenv
GEMINI_API_KEY=your-key
EXEC_AGENT_MODEL=model to use for AnalysisAgent (LiteLLM format: e.g., gemini/gemini-3-flash-preview)
SPECIMIN=(optional path to a local copy of Specimin)
REFACTOR_AGENT_MODEL=model to use for refactoring (PydanticAI format: e.g., google:gemini-3-flash-preview)
REFACTOR_AGENT_MAX_RPM=max requests per minute for refactoring agent
CHECKER_FRAMEWORK_VERSION=the CF version to use
MAX_PROCESSES=the maximum number of concurrent processes to run (default: os.cpu_count())
```

## Runtime requirements

- Docker installed, available on `PATH`, and running.
- A POSIX-style environment (Linux/macOS, or WSL on Windows).

## Usage

```bash
py src/main.py -c checkers.txt -t targets.jsonl
```

- `checkers.txt`: one fully-qualified Checker Framework checker per line, for example:

  ```text
  org.checkerframework.checker.nullness.NullnessChecker
  org.checkerframework.checker.resourceleak.ResourceLeakChecker
  org.checkerframework.checker.interning.InterningChecker
  ```

- `targets.jsonl`: one target repository per line, for example:

  ```json lines
  {"name": "jopt-simple", "url": "https://github.com/jopt-simple/jopt-simple"}
  ```

## Logs and outputs

- Pipeline run logs: `logs/run_<timestamp>/`
- Failed minimizations: `logs/run_<timestamp>/failed_minimizations.jsonl` and `.log`
- Failed compilations: `logs/run_<timestamp>/failed_compilations.jsonl` and `.log`
- Failed preservations: `logs/run_<timestamp>/failed_preservations.jsonl` and `.log`
- Unhandled crashes: `logs/run_<timestamp>/crashes.jsonl` and `.log`
- Refactor outputs: `logs/run_<timestamp>/refactor_results.jsonl` and `.log`

## Contributor/agent guidance

See [`AGENTS.md`](./AGENTS.md) for repository-specific expectations.