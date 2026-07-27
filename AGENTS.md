# AGENTS

This repository is maintained for the `false-positives-miner` pipeline.

## Scope
- Keep changes focused on the requested task.
- Avoid broad refactors unless they are necessary to unblock the task.

## Setup and execution
- Install dependencies with `uv sync`.
- Provide required environment variables in `.env` (see `README.md`).
- Run the tool with `py src/main.py -c checkers.txt -t targets.jsonl`.

## Logging and outputs
- Runtime logs are written under `logs/run_<timestamp>/`.
- Structured failure logs are written as JSONL and readable `.log` companions.

## Safety and quality expectations
- Preserve current behavior unless a task explicitly asks for behavior changes.
- Prefer small, reviewable commits.
- Update README and inline documentation when behavior or usage details change.
