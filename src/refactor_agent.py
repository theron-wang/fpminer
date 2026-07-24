import json
import logging
import os
import shutil
import time
from pathlib import Path

import checker_framework
from attr import dataclass
from differential_tester import DifferentialTester
from java_parser import get_method_text_for_signature
from pydantic_ai import Agent, UsageLimits, RunContext
from rate_limiter import GLOBAL_MODEL_RATE_LIMITER
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
)
from utils import read_file_with_numbered_lines, CheckerError, \
    run_checker_and_parse_errors, find_source_dir

logger = logging.getLogger(__name__)

MAX_FINISH_ATTEMPTS = 3
REQUEST_LIMIT = 20

# Minimum seconds between two run_checker() calls from the *same* agent
# session. This forces the model to actually batch edits (as the prompt
# already asks it to) instead of calling run_checker() after every single
# `edit_target_method`, which was one of the biggest sources of wasted requests.
MIN_SECONDS_BETWEEN_CHECKER_RUNS = 8


def _is_rate_limit_or_service_unavailable_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status in (429, 503):
        return True

    msg = str(exc).lower()
    return any(
        needle in msg
        for needle in (
            "rate limit",
            "rate_limit",
            "429",
            "resource_exhausted",
            "resource exhausted",
            "quota",
            "too many requests",
            "503",
            "service unavailable",
            "unavailable",
            "overloaded",
        )
    )


@dataclass
class RefactorAgentRun:
    success: bool = False
    possible: bool = False
    error: str | None = None
    not_possible_reason: str | None = None


class RefactorAgent:
    def __init__(self, orig_directory: Path, checker_name: str,
                 error: CheckerError,
                 errors_to_ignore: list[CheckerError],
                 target_method_signature: str,
                 path_to_full_project: Path,
                 differential_tester: DifferentialTester):
        self.orig_directory = orig_directory
        self.modified_directory = orig_directory.parent / "modified"
        self.diff_testing_dir = orig_directory.parent / "diff_testing"
        self.differential_tester = differential_tester

        self.run_checker_cmd = checker_framework.get_command_for_checker(checker_name, self.modified_directory)
        self.checker_name = checker_name.split('.')[-1]
        self.target_method_signature = target_method_signature
        self.path_to_full_project = path_to_full_project

        # if os.path.exists(self.modified_directory):
        #     shutil.rmtree(self.modified_directory)
        #
        # shutil.copytree(str(self.orig_directory), self.modified_directory)

        error.file_path = error.file_path.replace(str(orig_directory), str(self.modified_directory))
        error.raw = error.raw.replace(str(orig_directory), str(self.modified_directory))

        for e in errors_to_ignore:
            e.file_path = e.file_path.replace(str(orig_directory), str(self.modified_directory))
            e.raw = e.raw.replace(str(orig_directory), str(self.modified_directory))

        self.error_to_fix = error
        self.errors_to_ignore = errors_to_ignore

        # The mirror copy is the target file's original content from the original repository, not
        # from the Speciminified output
        rel_path = Path(self.error_to_fix.file_path).resolve().relative_to(self.modified_directory.resolve())
        self.mirror_copy_for_differential_testing = (
                self.diff_testing_dir / rel_path).resolve()

        os.makedirs(self.mirror_copy_for_differential_testing.parent, exist_ok=True)

        full_project_src_dir = find_source_dir(path_to_full_project, rel_path)

        assert full_project_src_dir is not None

        shutil.copy(full_project_src_dir / rel_path, self.mirror_copy_for_differential_testing)

        self.agent = Agent(
            os.environ["REFACTOR_AGENT_MODEL"]
        )

        self.finished = False
        self.success = False
        self.possible = True
        self.finish_attempts = 0
        self.not_possible_reason = None
        self._last_checker_run_time: float | None = None

        @self.agent.tool
        def run_checker(ctx: RunContext[None]) -> str:
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

            IMPORTANT - this is comparatively expensive. Do not call it after
            every individual edit. Batch several related `edit_target_method` calls
            together first, then call this once to check the whole batch.
            If you call this again too soon after a previous call, it will
            be rejected without running the checker.
            """
            # Every tool call sits between two model requests (the one that
            # produced this call, and the one that will fire once we return
            # a result). Throttling here paces the *next* request, since
            # run_sync's internal loop gives us no other hook to gate on.
            GLOBAL_MODEL_RATE_LIMITER.acquire()

            if self._last_checker_run_time is not None:
                elapsed = time.monotonic() - self._last_checker_run_time
                if elapsed < MIN_SECONDS_BETWEEN_CHECKER_RUNS:
                    wait_left = MIN_SECONDS_BETWEEN_CHECKER_RUNS - elapsed
                    return json.dumps({
                        "success": False,
                        "error_count": -1,
                        "errors": [],
                        "message": (
                            f"run_checker() was called too soon after the previous "
                            f"call ({elapsed:.1f}s ago). Make more edits first and "
                            f"batch them - try again in about {wait_left:.0f}s worth "
                            "of additional work, not by waiting idly."
                        ),
                    })

            self._last_checker_run_time = time.monotonic()
            errors = self._run_checker()

            payload = {
                "success": len(errors) == 0,
                "error_count": len(errors),
                "errors": errors,
            }

            return json.dumps(payload)

        @self.agent.tool
        def check_semantic_equivalence(ctx: RunContext[None]) -> str:
            """Run full differential testing to verify your edit preserves semantic
            equivalence with the original code.

            This is EXTREMELY EXPENSIVE - it is orders of magnitude more costly than
            `run_checker` or `edit_target_method`, since it runs a differential fuzzer.
            Do not call this speculatively, experimentally, or as a substitute for reasoning
            through your edit yourself.

            Only call this once, after you have:
              1. Made the edit(s) you believe fix the checker error.
              2. Confirmed via `run_checker` that the checker now passes.
              3. Manually reviewed your diff and convinced yourself it is a real
                 code-level change (not an annotation-only change) that preserves
                 behavior for all previously-valid inputs.

            This is a final verification step, not a search tool - you should already
            be confident the edit is correct before calling it. Calling it multiple
            times per error, or calling it before `run_checker` has confirmed the
            checker passes, wastes a very large amount of compute and is HIGHLY
            DISCOURAGED except when genuinely necessary to resolve ambiguity you
            cannot otherwise resolve.

            Returns a JSON string:
              {"success": bool, "message": str}
            """
            GLOBAL_MODEL_RATE_LIMITER.acquire()

            return json.dumps(self._run_differential_testing())

        @self.agent.tool
        def view_file(ctx: RunContext[None], relative_path: str) -> str:
            """View the current contents of a file in the modified directory,
            with line numbers prefixed followed by a tab (i.e., '12\tsome code').
            Line numbers are for your reference only - do NOT include them
            when constructing old_str for `edit_target_method`.

            You already have the full numbered contents of every file from
            the initial prompt, and `edit_target_method`'s success response includes
            fresh context around each edit you make. Only call this if a
            `edit_target_method` call failed (old_str not found / not unique) and you
            need to re-sync on a file's true current contents - do not call
            it routinely before edits you're already confident about.
            """
            GLOBAL_MODEL_RATE_LIMITER.acquire()

            path = (self.modified_directory / relative_path).resolve()
            if not str(path).startswith(str(self.modified_directory.resolve())):
                return f"Error: path escapes modified directory: {relative_path}"
            if not path.exists():
                return f"Error: file not found: {relative_path}"

            numbered = read_file_with_numbered_lines(path)
            return numbered

        @self.agent.tool
        def view_original_file(ctx: RunContext[None], relative_path: str) -> str:
            """View the original contents of a file in the original directory,
            with line numbers prefixed followed by a tab (i.e., '12\tsome code').
            Line numbers are for your reference only.

            You should only call this if the semantic-equivalence checker fails,
            and you need to refresh your memory on the original method.
            """
            GLOBAL_MODEL_RATE_LIMITER.acquire()

            path = (self.orig_directory / relative_path).resolve()
            if not str(path).startswith(str(self.orig_directory.resolve())):
                return f"Error: path escapes original directory: {relative_path}"
            if not path.exists():
                return f"Error: file not found: {relative_path}"

            numbered = read_file_with_numbered_lines(path)
            return numbered

        @self.agent.tool
        def edit_target_method(ctx: RunContext[None], old_str: str, new_str: str) -> str:
            """Replace an exact block of text in the target method with new text.

            old_str must match a part of the target method's raw content EXACTLY
            (no line number prefixes, exact whitespace) and must appear exactly once
            in the target method. If it appears zero times or more than once, the edit is
            rejected and no changes are made - in that case, the error message will
            contain the updated method content which you should use. Then, widen old_str
            with a bit more surrounding context (i.e., an extra line above/below) until
            it is unique.

            Returns a JSON string:
              {"success": bool, "message": str}

            On success, message includes a few lines of context around the
            edit so you can confirm the change landed correctly - use this
            instead of calling view_file again on the same file.
            """
            GLOBAL_MODEL_RATE_LIMITER.acquire()

            path_to_workspace_copy = Path(self.error_to_fix.file_path)

            content = get_method_text_for_signature(path_to_workspace_copy, self.target_method_signature)
            assert content is not None
            count = content.count(old_str)

            if count == 0:
                return json.dumps({
                    "success": False,
                    "message": (
                        "old_str not found in the target method. Its contents may have "
                        "changed since you last viewed it, or whitespace doesn't match "
                        "exactly."
                        "\n\n"
                        f"`{self.target_method_signature}`:"
                        "```java"
                        f"{content}"
                        "```"
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

            new_method_content = content.replace(old_str, new_str)

            file_content = path_to_workspace_copy.read_text()
            new_file_content = file_content.replace(content, new_method_content, 1)

            path_to_workspace_copy.write_text(new_file_content)

            # Build a small context window around the edit for confirmation.
            new_lines = new_file_content.splitlines()
            replaced_start = content[:content.index(old_str)].count("\n")
            ctx_start = max(0, replaced_start - 2)
            ctx_end = min(len(new_lines), replaced_start + new_str.count("\n") + 3)
            context = "\n".join(
                f"{i + 1:d}\t{new_lines[i]}" for i in range(ctx_start, ctx_end)
            )

            # Now, update the mirror copy for when the agent needs to call the diff tester
            file_content = self.mirror_copy_for_differential_testing.read_text()
            new_file_content = file_content.replace(content, new_method_content, 1)
            self.mirror_copy_for_differential_testing.write_text(new_file_content)

            return json.dumps({
                "success": True,
                "message": f"Edit applied. Context around the change:\n{context}",
            })

        @self.agent.tool
        def not_possible(ctx: RunContext[None], reason: str) -> str:
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
            GLOBAL_MODEL_RATE_LIMITER.acquire()

            self.finished = True
            self.possible = False
            self.not_possible_reason = reason
            return json.dumps({
                "finished": True,
                "possible": False,
                "reason": reason,
            })

        @self.agent.tool
        def finish(ctx: RunContext[None]) -> str:
            """Call this ONLY when you are confident your assigned target error is
            resolved. This re-runs the checker to verify specifically that
            your target error is gone, and that additional bugs were not introduced.
            However, other pre-existing errors elsewhere in the codebase do not block
            this from succeeding. This will also run the semantic-equivalence checker
            to ensure that your edit preserves the original method's behavior.

            If your target error is still present, the session will NOT end and
            you should keep working. However, finish() may only be called a
            limited number of times. If you call it repeatedly without actually
            resolving the target error, the session will be forcibly terminated
            as a failure - so do not call finish() speculatively or as a way to
            "check in." Only call it once you are confident that your edit resolves
            the error, does not introduce any additional errors, and preserves
            the semantics of the original method.

            Returns:
                JSON string: {"finished": bool, "success": bool,
                               "error_count": int, "errors": [...], "message": str}
            """
            GLOBAL_MODEL_RATE_LIMITER.acquire()

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
                        "edits with `edit_target_method`, and call finish() again once "
                        "resolved."
                    ),
                })

            self.finished = True
            self.success = True
            return json.dumps({
                "finished": True,
                "success": True,
                "error_count": 0,
                "errors": [],
                "message": "Checker passed with zero errors. Session complete.",
            })

    def run(self) -> RefactorAgentRun:
        """
        Runs the agent on the given error.
        """
        initial_prompt = self._build_initial_prompt()

        result = RefactorAgentRun()

        try:
            self._run_sync_with_rate_limit_retry(initial_prompt)
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

    @retry(
        retry=retry_if_exception(_is_rate_limit_or_service_unavailable_error),
        wait=wait_exponential_jitter(initial=2, max=60, jitter=3),
        stop=stop_after_attempt(6),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _run_sync_with_rate_limit_retry(self, prompt: str):
        # Proactively throttle before every top-level run - blocks until a
        # token is available instead of firing and hoping.
        GLOBAL_MODEL_RATE_LIMITER.acquire()

        return self.agent.run_sync(
            prompt, usage_limits=UsageLimits(request_limit=REQUEST_LIMIT)
        )

    def _run_checker(self) -> list[str]:
        errors = run_checker_and_parse_errors(self.run_checker_cmd, self.modified_directory)
        errors = [
            error.raw
            for error in errors
            if error.is_compilation_error() or not any(error.likely_equals(e) for e in self.errors_to_ignore)
        ]

        return errors

    def _run_differential_testing(self) -> str:
        pass

    def _build_initial_prompt(self) -> str:
        """
        Builds the initial prompt for the agent, including the task description,
        the files in the project, and the specific error to fix.
        :return: the prompt
        """
        file_blocks = []
        for path in sorted(self.modified_directory.rglob("*.java")):
            rel_path = path.relative_to(self.modified_directory)
            numbered = read_file_with_numbered_lines(path)
            file_blocks.append(f"### {rel_path}\n```java\n{numbered}\n```")

        files_section = "\n\n".join(file_blocks)

        return f"""You are fixing Checker Framework {self.checker_name} errors in a Java project.

## Task
The project above fails Checker Framework's {self.checker_name} Checker with 
exactly ONE error:

{self.error_to_fix.raw}

Your job is to refactor the code so that:

1. The checker passes with zero errors.
2. The refactor is semantically equivalent to the original code - it must
   preserve the exact same runtime behavior for all valid inputs. You are
   fixing a *type-checking* problem, not changing what the program does.
3. The fix must be an actual code change, not an annotation-only change.
   Adding, removing, or moving a type annotation - with no other
   change to the surrounding code - is NOT an acceptable fix, even if it
   makes the checker pass. Annotations describe the code; they are not
   themselves a semantics-preserving code change, and this task is
   specifically about finding a code-level refactor. If the only way you
   can find to satisfy the checker is to add/remove/relocate an annotation
   with the logic otherwise untouched, treat that the same as having no
   fix: call `not_possible` and explain that the only fix you found was
   annotation-only.

Semantically equivalent means, at minimum:
- No change to public method signatures' behavior as observed by callers
  (return values, thrown exceptions, side effects) for any input that was
  previously valid.
- No change to initializer values. That is, a variable or field previously
  set to a specific value (like null) should stay that value.
- Absolutely no suppression of the error via @SuppressWarnings. A suppression
  is not a fix, it's hiding the question.
- No deletion of the logic that produces the warning in order to make the
  warning disappear. Removing or adding a null check, a field, a throw,
  or a code path is not a refactor, it's a behavior change.
- Prefer the smallest, most localized, semantics-preserving transformation.
  Avoid changes to unrelated code, broad refactorings, or unnecessary annotation
  changes.

While working through this codebase, you may notice code that would also
trigger this checker. You must leave it alone - it is out of scope for
this task even if it looks like an easy fix. `run_checker` accounts for
these cases, so you can always trust its output.

If, after investigating, you determine there is NO refactor that both (a)
fixes the error and (b) preserves semantic equivalence - for example, the
error reflects a genuine possible null dereference that the original code
never actually guarded against, and any real fix would necessarily change
behavior on some input - do not force a fix. Call `not_possible` with an
explanation of why. Likewise, if the only way you can make the checker pass
is by changing annotations alone with no accompanying code change, that
also counts as no fix being possible - call `not_possible` rather than
submitting an annotation-only edit. Do not attempt to disguise a behavior
change, or a bare annotation change, as a "fix" merely to make the checker
pass.

## Making code edits

You will notice that most definitions have been stubbed out. This is
by design. There will be one non-stubbed method - **the target method** -
which is the method (or constructor) that contains the error. This is the
ONLY code which you can change. You may not change any other method, constructor,
type, or field in the project provided to you. The target method's signature is as
follows:

{self.target_method_signature}

## Files in the project
The line numbers shown below are for your reference only - do NOT include
them when constructing old_str for `edit_target_method`. All paths are relative to
the project root. You already have the full current contents of every file
right here - do not call `view_file` for a file you haven't edited yet.
In your edits, you may assume that the Checker Framework is on the classpath.

{files_section}

## How to work
1. For the given error, make the smallest edit that fixes it using `edit_target_method`.
   Calling `edit_target_method` with your edit MUST be your FIRST action, since you have
   already been given the error you need to fix, alongside the files in this
   project. You shall not call `view_file` or `run_checker` before trying
   `edit_target_method` at least once. Trust the numbered file contents above as ground
   truth until you have edited a file yourself. If old_str is rejected as not
   found or not unique, you will get the contents of the target method; treat that
   as fact and use it to make your edit. Recall that you may only edit code
   in the target method.
2. Batch several related edits together before calling `run_checker` -
   it's comparatively expensive, so don't call it after every single
   `edit_target_method`. Only call it once you believe you've made all the edits
   needed for this batch. Ensure that you do not introduce additional
   Checker Framework errors/warnings and that you do not cause the code
   to be uncompilable.
3. Before calling `run_checker` or `finish`, check your diff against the
   target method: if the only lines that changed are annotations (nothing
   about control flow, expressions, assignments, or structure), that is not
   a valid fix - revert to a real code-level approach or call `not_possible`.
4. Once you believe all errors are resolved, call `finish`. `finish`
   re-runs the checker itself - if errors remain, you'll get them back and
   must keep working. Do not call `finish` until you have already confirmed
   via `run_checker` that you expect it to pass. `finish` may only be
   called a limited number of times, so don't call it speculatively.
5. If `finish` requests that you try again because your refactor is not
   semantically equivalent, you may call `check_semantic_equivalence` once
   you are ABSOLUTELY CERTAIN your edits pass the checker and preserve
   semantic equivalence. `check_semantic_equivalence` is extremely expensive,
   so you should call it very sparingly. You should check the original file
   contents with `view_original_file` if you are unsure what the old method
   definition was.
5. Do not guess at file contents from memory beyond what's shown above.
   Every edit must be based on content you have actually seen in this
   session - the listing above, a `view_file` call, or a `edit_target_method`
   confirmation.
"""
