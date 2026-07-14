# false-positives-miner

---

To setup:

```bash
uv sync
```

Create a `.env` file in the directory root:

```
GEMINI_API_KEY=your-key
EXEC_AGENT_MODEL=model to use
SPECIMIN=(optional path to a local copy of Specimin)
```

**NOTE:** This tool requires Docker. Ensure it is installed, on PATH, and running.
You must also ensure that this script is being run in a POSIX-style environment.
If you are using Windows, run this in WSL.

To run:

```bash
py src/main.py -c checkers.txt -t targets.jsonl
```

Where `checkers.txt` is a list of fully-qualified Checker Framework checkers:
```
org.checkerframework.checker.nullness.NullnessChecker
org.checkerframework.checker.resourceleak.ResourceLeakChecker
org.checkerframework.checker.interning.InterningChecker
```

And `targets.jsonl` is a list of target repositories:
```json lines
{"name": "jopt-simple", "url": "https://github.com/jopt-simple/jopt-simple"}
```

You may view AnalysisAgent run logs in `/logs`.

Specimin logs are in multiple locations:
* Failed minimizations: `./failed_minimizations.txt`
* Failed compilations: `./workspace/{target name}/{tool name}/{error #}/failed_compilations.txt`
* Failed preservations: `./workspace/{target name}/{tool name}/{error #}/failed_preservations.txt`