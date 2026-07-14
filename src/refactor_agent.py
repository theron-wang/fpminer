import json
import os
import shutil
import subprocess
from pathlib import Path

from attr import dataclass
from utils import parse_errors_from_checker_output, read_file_with_numbered_lines, CheckerError

from pydantic_ai import Agent, UsageLimits

MAX_FINISH_ATTEMPTS = 5

@dataclass
class RefactorAgentRun:
    success: bool = False
    possible: bool = False
    error: str | None = None
    not_possible_reason: str | None = None

class RefactorAgent:
    def __init__(self, orig_directory: Path, run_checker_cmd: list[str], checker_name: str, errors_to_ignore: list[CheckerError]):
        self.orig_directory = orig_directory
        self.modified_directory = orig_directory / "../modified"
        self.run_checker_cmd = run_checker_cmd
        self.checker_name = checker_name.split('.')[-1]
        self.errors_to_ignore = set([err.content for err in errors_to_ignore])

        if os.path.exists(self.modified_directory):
            shutil.rmtree(self.modified_directory)

        shutil.copytree(str(self.orig_directory), self.modified_directory)

        self.agent = Agent(
            "gemini:gemini-3.1-flash-lite"
        )

        self.finished = False
        self.success = False
        self.possible = True
        self.finish_attempts = 0
        self.not_possible_reason = None

        @self.agent.tool
        def run_checker() -> str:
            """Runs the checker analysis on the modified
            directory and return a JSON string describing the result.

            Returns a JSON object of the form:
              {
                "success": bool,
                "error_count": int,
                "errors": [
                  {"file_path": str, "line_number": int, "content": str},
                  ...
                ]
              }

            success is true only if the build produced zero errors.
            Always call this after making edits to verify they didn't
            break compilation or introduce new errors.
            """
            errors = self._run_checker()

            payload = {
                "success": len(errors) == 0,
                "error_count": len(errors),
                "errors": errors,
            }

            return json.dumps(payload)

        @self.agent.tool
        def view_file(relative_path: str) -> str:
            """View the current contents of a file in the modified directory,
            with line numbers prefixed followed by a tab (i.e., '12\tsome code').
            Line numbers are for your reference only - do NOT include them
            when constructing old_str for str_replace.

            You MUST call this immediately before str_replace on a file if
            you haven't viewed it in your most recent turn, since prior edits
            may have changed its contents.
            """
            path = (self.modified_directory / relative_path).resolve()
            if not str(path).startswith(str(self.modified_directory.resolve())):
                return f"Error: path escapes modified directory: {relative_path}"
            if not path.exists():
                return f"Error: file not found: {relative_path}"

            numbered = read_file_with_numbered_lines(path)
            return numbered

        @self.agent.tool
        def str_replace(relative_path: str, old_str: str, new_str: str) -> str:
            """Replace an exact block of text in a file with new text.

            old_str must match the file's raw content EXACTLY (no line number
            prefixes, exact whitespace) and must appear exactly once in the
            file. If it appears zero times or more than once, the edit is
            rejected and no changes are made - in that case, call view_file
            again to get fresh content and widen old_str with a bit more
            surrounding context (i.e., an extra line above/below) until it is
            unique.

            Returns a JSON string:
              {"success": bool, "message": str}

            On success, message includes a few lines of context around the
            edit so you can confirm the change landed correctly.
            """
            path = (self.modified_directory / relative_path).resolve()
            if not str(path).startswith(str(self.modified_directory.resolve())):
                return json.dumps({
                    "success": False,
                    "message": f"path escapes modified directory: {relative_path}",
                })
            if not path.exists():
                return json.dumps({
                    "success": False,
                    "message": f"file not found: {relative_path}",
                })

            content = path.read_text()
            count = content.count(old_str)

            if count == 0:
                return json.dumps({
                    "success": False,
                    "message": (
                        "old_str not found in file. The file may have changed "
                        "since you last viewed it, or whitespace doesn't match "
                        "exactly. Call view_file again and copy old_str precisely."
                    ),
                })
            if count > 1:
                return json.dumps({
                    "success": False,
                    "message": (
                        f"old_str is not unique ({count} occurrences). Add more "
                        "surrounding context (a line above or below) so it "
                        "matches exactly one location."
                    ),
                })

            new_content = content.replace(old_str, new_str, 1)
            path.write_text(new_content)

            # Build a small context window around the edit for confirmation.
            new_lines = new_content.splitlines()
            replaced_start = content[:content.index(old_str)].count("\n")
            ctx_start = max(0, replaced_start - 2)
            ctx_end = min(len(new_lines), replaced_start + new_str.count("\n") + 3)
            context = "\n".join(
                f"{i+1:d}\t{new_lines[i]}" for i in range(ctx_start, ctx_end)
            )

            return json.dumps({
                "success": True,
                "message": f"Edit applied. Context around the change:\n{context}",
            })

        @self.agent.tool
        def not_possible(reason: str) -> str:
            """Call this if, after investigation, you conclude there is no way to
            fix the checker error(s) while keeping the code semantically
            equivalent to the original - i.e., any fix you can find would change
            runtime behavior on some input, not just satisfy the type checker.

            Do NOT call this just because a fix is difficult or would take several
            edits. Only call it when you believe a semantics-preserving fix does
            not exist. This ends the session immediately; no further tool calls
            will be processed.

            Args:
                reason: A precise technical explanation of why no semantics-
                    preserving fix exists - what you tried or considered, and
                    specifically what behavior would have to change and for
                    which inputs.

            Returns:
                JSON string: {"finished": true, "possible": false, "reason": str}
            """
            self.finished = True
            self.possible = False
            self.not_possible_reason = reason
            return json.dumps({
                "finished": True,
                "possible": False,
                "reason": reason,
            })

        @self.agent.tool
        def finish() -> str:
            """Call this ONLY when you believe your assigned target error is
            resolved. This re-runs the checker and verifies specifically that
            your target error is gone - other pre-existing errors elsewhere in
            the codebase do not block this from succeeding.

            If your target error is still present, the session will NOT end and
            you should keep working. However, finish() may only be called a
            limited number of times. If you call it repeatedly without actually
            resolving the target error, the session will be forcibly terminated
            as a failure - so do not call finish() speculatively or as a way to
            "check in." Only call it once you have already confirmed via
            run_checker() that you expect it to pass.

            Returns:
                JSON string: {"finished": bool, "success": bool,
                               "error_count": int, "errors": [...], "message": str}
            """
            self.finish_attempts += 1
            errors = self._run_checker()

            if errors:
                self.finished = False

                if self.finish_attempts >= MAX_FINISH_ATTEMPTS:
                    self.finished = True
                    self.success = False

                    return json.dumps({
                        "finished": True,
                        "success": False,
                        "error_count": len(errors),
                        "errors": errors,
                        "message": (
                            "Maximum finish() attempts exceeded. The session is now "
                            "being forcibly ended without a resolved target error."
                        ),
                    })

                return json.dumps({
                    "finished": False,
                    "success": False,
                    "error_count": len(errors),
                    "errors": errors,
                    "message": (
                        f"Checker still reports {len(errors)} error(s). The session "
                        "is NOT complete. Review the errors below, make further "
                        "edits with str_replace, and call finish() again once "
                        "resolved."
                    ),
                })

            self.finished = True
            return json.dumps({
                "finished": True,
                "success": True,
                "error_count": 0,
                "errors": [],
                "message": "Checker passed with zero errors. Session complete.",
            })

    def run(self, error: CheckerError) -> RefactorAgentRun:
        """
        Runs the agent on the given error.
        :param error: The error to fix. Ensure that you rerun on the Speciminified project before passing this in.
        """
        initial_prompt = self._build_initial_prompt(error)

        result = RefactorAgentRun()

        try:
            self.agent.run(initial_prompt, usage_limits=UsageLimits(request_limit=40))
        except Exception as e:
            result.error = f"Agent run failed or exhausted limits: {e}"
            return result

        # agent.run() returned normally, but that doesn't guarantee finish()
        # or not_possible() was ever actually called - the model may have
        # just stopped producing tool calls.
        if not self.finished:
            result.error = (
                "Agent stopped without calling finish() or not_possible()."
            )
        else:
            result.success = self.success
            result.possible = self.possible
            result.not_possible_reason = self.not_possible_reason

        return result

    def _run_checker(self) -> list[CheckerError]:
        result = subprocess.run(
            self.run_checker_cmd, capture_output=True, text=True
        )

        errors = parse_errors_from_checker_output(result.stderr) if result.returncode != 0 else []
        errors = [error for error in errors if error.content not in self.errors_to_ignore]

        return errors

    def _build_initial_prompt(self, error: CheckerError) -> str:
        """
        Builds the initial prompt for the agent, including the task description,
        the files in the project, and the specific error to fix.
        :param error: The error. Ensure that you rerun on the Speciminified project before passing this in.
        :return: the prompt
        """
        file_blocks = []
        for path in sorted(self.modified_directory.rglob("*.java")):
            rel_path = path.relative_to(self.modified_directory)
            numbered = read_file_with_numbered_lines(path)
            file_blocks.append(f"### {rel_path}\n```java\n{numbered}\n```")

        files_section = "\n\n".join(file_blocks)

        error_as_string = f"{error.file_path}:{error.line_number}: {error.content}"

        return f"""You are fixing Checker Framework {self.checker_name} errors in a Java project.

## Task
The project above fails Checker Framework's {self.checker_name} Checker with 
exactly ONE error: {error_as_string}

Your job is to refactor the code so that:

1. The checker passes with zero errors.
2. The refactor is semantically equivalent to the original code - it must
   preserve the exact same runtime behavior for all valid inputs. You are
   fixing a *type-checking* problem, not changing what the program does.

Semantically equivalent means, at minimum:
- No change to public method signatures' behavior as observed by callers
  (return values, thrown exceptions, side effects) for any input that was
  previously valid.
- Absolutely no suppression of the error via @SuppressWarnings, A suppression
  is not a fix, it's hiding the question.
- No deletion of the logic that produces the warning in order to make the
  warning disappear. Removing or adding a null check, a field, or a code
  path is not a refactor, it's a behavior change.
- Prefer the most localized fix: a narrowing check, a corrected annotation
  (@Nullable / @NonNull), an Optional where the codebase already uses
  Optional, or a small restructuring of control flow - over anything that
  touches unrelated code.

While working through this codebase, you may notice code that would also
trigger this checker. You must leave it alone - it is out of scope for
this task even if it looks like an easy fix. run_checker() accounts for
these cases, so you can always trust its output.

If, after investigating, you determine there is NO refactor that both (a)
fixes the error and (b) preserves semantic equivalence - for example, the
error reflects a genuine possible null dereference that the original code
never actually guarded against, and any real fix would necessarily change
behavior on some input - do not force a fix. Call not_possible() with an
explanation of why. Do not attempt to disguise a behavior change as a
"fix" merely to make the checker pass.

## Files in the project
The line numbers shown below are for your reference only - do NOT include
them when constructing old_str for str_replace. All paths are relative to
the project root.

{files_section}

## How to work
1. For the given error, make the smallest edit that fixes it using str_replace.
   If old_str is rejected as not found or not unique, call view_file on
   that specific file to get fresh content before retrying.
2. After a batch of related edits, call run_checker() again to check
   progress before continuing. Ensure you do not introduce additional
   Checker Framework errors/warnings and that you do not cause the code
   to be uncompilable.
3. Once you believe all errors are resolved, call finish(). finish()
   re-runs the checker itself - if errors remain, you'll get them back and
   must keep working. Do not call finish() until you have already confirmed
   via run_checker() that you expect it to pass.
4. Do not guess at file contents from memory. Every edit must be based on
   content you have actually viewed in this session, either from the
   listing above or a subsequent view_file call.
"""