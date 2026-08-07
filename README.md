# FPMiner

A pipeline for mining and reducing Checker Framework false positive patterns across target Java repositories.

FPMiner runs in two steps:

1. **Analysis** — For each target repository, configure and run the specified Checker Framework checkers to identify false positives.
2. **Refactoring** — For each error found during analysis, a refactoring agent generates a patch that removes the checker error while preserving the original semantics of the code. This step is parallelized, so increasing the number of CPU cores available to the process will greatly improve output speeds.

---

## Setup

Install dependencies:

```bash
uv sync
```

Create a `.env` file in the repository root:

```dotenv
# LLM API key, based on the models you use
# For example:
GEMINI_API_KEY=your-key
OPENAI_API_KEY=your-key
ANTHROPIC_API_KEY=your-key

# Model used by the AnalysisAgent (LiteLLM format).
# Example: gemini/gemini-3-flash-preview
EXEC_AGENT_MODEL=gemini/gemini-3-flash-preview

# Model used for refactoring (PydanticAI format).
# Example: google:gemini-3-flash-preview
REFACTOR_AGENT_MODEL=google:gemini-3-flash-preview

# Maximum requests per minute for the refactoring agent.
# Default: 15
REFACTOR_AGENT_MAX_RPM=15

# Optional path to a local Specimin installation.
SPECIMIN=

# Checker Framework version to use.
# Default: 4.2.1
CHECKER_FRAMEWORK_VERSION=4.2.1

# Maximum number of concurrent processes.
# Default: os.cpu_count()
MAX_PROCESSES=
```

### Runtime requirements

- A POSIX-style environment (Linux/macOS, or WSL on Windows)
- Docker installed, available on `PATH`, and running
- `git` installed and available on `PATH`
- A valid Java installation (Java 17+)

---

## Usage

```bash
py src/main.py -c checkers.txt -t targets.jsonl
```

| Argument | Description |
|---|---|
| `checkers.txt` | One fully-qualified Checker Framework checker per line |
| `targets.jsonl` | One target repository per line (JSON Lines format) |

**`checkers.txt` example:**

```text
org.checkerframework.checker.nullness.NullnessChecker
org.checkerframework.checker.resourceleak.ResourceLeakChecker
org.checkerframework.checker.index.IndexChecker
org.checkerframework.checker.optional.OptionalChecker
```

**`targets.jsonl` example:**

```json lines
{"name": "jopt-simple", "url": "https://github.com/jopt-simple/jopt-simple"}
```

---

## Output

All output, including logs and results, is stored in the `results/run_<timestamp>/` directory.

### Logs

| Log | Path |
|---|---|
| Run logs | `results/run_<timestamp>/run.log` |
| Crash logs | `results/run_<timestamp>/crashes.log` |
| Specimin failure logs | `results/run_<timestamp>/specimin_failures.log` |
| Run summary | `results/run_<timestamp>/summary.json` |

### Diffs

Each target/checker pair creates a subdirectory at `results/run_<timestamp>/output/<target>/<checker>`, containing:

| File | Description |
|---|---|
| `failure/annotation_only.txt` | Refactor agent runs whose refactored code only changed annotations, where the agent was not able to resolve the checker error |
| `failure/other_errors.txt` | Refactor agent runs that failed for other reasons — e.g. the agent ran out of attempts to produce semantically-equivalent code, the LLM was rate-limited, or another crash occurred |
| `success/false_positives.txt` | Refactor agent runs that successfully resolved the checker error and produced semantically-equivalent code |
| `success/annotation_only.txt` | Refactor agent runs that successfully resolved the checker error, but only changed annotations |
| `unresolved/inconclusive.txt` | Runs where the differential tester was unable to determine if the refactored code was semantically equivalent to the original |
| `unresolved/not_possible.txt` | Runs where the agent declared it was not possible to create semantics-preserving code that resolved the checker error |

### Artifacts

#### Refactor agent

The refactor agent leaves behind artifacts in the `workspace/` directory:

```
workspace/
├── <target>/
│   ├── <checker>/
│       ├── <error_number>/
│           ├── orig/          # original Speciminified code
│           ├── modified/      # refactored code
│           ├── diff_testing/  # target method with the full file from the original codebase
│   ├── <target>/              # copy of the target repository
```

#### Tools

Used during the mining process and cloned into the `tools/` directory:

- [The Checker Framework](https://checkerframework.org/)
- [Differential-Test-Fuzzing](https://github.com/musta55/Differential-Fuzz-Testing)
- [Specimin](https://github.com/njit-jerse/specimin)

#### Targets

Target repositories are cloned into the `targets/` directory, set up for analysis and refactoring.

#### AnalysisAgent

AnalysisAgent leaves behind logs and workspace artifacts in the `analysis_agent/` directory.

---

## Contributor / Agent Guidance

See [`AGENTS.md`](./AGENTS.md) for repository-specific expectations.