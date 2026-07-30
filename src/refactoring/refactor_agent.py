import json
import logging
import os
import shutil
from pathlib import Path

import checker_framework
from attr import dataclass
from differential_tester import DifferentialTester, DifferentialTestResult
from java.java_parser import get_method_text_for_signature
from pydantic_ai import Agent, UsageLimits, RunContext
from refactoring.rate_limiter import GLOBAL_MODEL_RATE_LIMITER
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
)
from utils import CheckerError, run_checker_and_parse_errors, find_source_dir

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
    orig_dir: Path
    modified_dir: Path
    differential_test_result: DifferentialTestResult
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
        self.path_to_full_project = path_to_full_project

        self.run_checker_cmd = checker_framework.get_command_for_checker(checker_name, self.modified_directory)
        self.checker_name = checker_name.split('.')[-1]
        self.target_method_signature = target_method_signature

        # if os.path.exists(self.modified_directory):
        #     shutil.rmtree(self.modified_directory)
        #
        # shutil.copytree(str(self.orig_directory), self.modified_directory)

        # Transpose the errors' files paths from the "orig" directory to the "modified" directory
        error.file_path = error.file_path.replace(str(orig_directory), str(self.modified_directory))
        error.raw = error.raw.replace(str(orig_directory), str(self.modified_directory))

        for e in errors_to_ignore:
            e.file_path = e.file_path.replace(str(orig_directory), str(self.modified_directory))
            e.raw = e.raw.replace(str(orig_directory), str(self.modified_directory))

        self.error_to_fix = error
        self.errors_to_ignore = errors_to_ignore

        # The mirror copy is the target file's original content from the original repository, not
        # from the Speciminified output
        self.target_rel_path = Path(self.error_to_fix.file_path).resolve().relative_to(
            self.modified_directory.resolve())
        self.mirror_copy_for_differential_testing = (
                self.diff_testing_dir / self.target_rel_path).resolve()

        os.makedirs(self.mirror_copy_for_differential_testing.parent, exist_ok=True)

        full_project_src_dir = find_source_dir(path_to_full_project, self.target_rel_path)

        assert full_project_src_dir is not None

        shutil.copy(full_project_src_dir / self.target_rel_path, self.mirror_copy_for_differential_testing)

        self.agent = Agent(
            os.environ["REFACTOR_AGENT_MODEL"]
        )

        self.finished = False
        self.success = False
        self.possible = True
        self.differential_test_result = DifferentialTestResult.INCONCLUSIVE
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
            """
            GLOBAL_MODEL_RATE_LIMITER.acquire()

            errors = self._run_checker()

            payload = {
                "success": len(errors) == 0,
                "error_count": len(errors),
                "errors": errors,
            }

            return json.dumps(payload)

        @self.agent.tool
        def view_original_target_method(ctx: RunContext[None]) -> str:
            """View the original content of the target method before any changes
            were made, for reference if the semantics-preserving checker in
            `finish()` fails.
            """
            GLOBAL_MODEL_RATE_LIMITER.acquire()

            return self._get_target_method_content()

        @self.agent.tool
        def edit_target_method(ctx: RunContext[None], old_str: str, new_str: str) -> str:
            """Replace an exact block of text in the target method with new text.

            old_str must match a part of the target method's raw content EXACTLY
            (no line number prefixes, exact whitespace) and must appear exactly once
            in the target method. If it appears zero times or more than once, the edit is
            rejected and no changes are made. Then, widen old_str with a bit more surrounding
            context (i.e., an extra line above/below) until it is unique.

            Returns a JSON string:
              {"success": bool, "message": str}

            A call to this tool, regardless of whether it ran successfully, will return the
            full content of the updated target method.
            """
            GLOBAL_MODEL_RATE_LIMITER.acquire()

            path_to_workspace_copy = Path(self.error_to_fix.file_path)

            content = get_method_text_for_signature(path_to_workspace_copy, self.target_method_signature)
            count = content.count(old_str)

            if count == 0:
                return json.dumps({
                    "success": False,
                    "message": (
                        "old_str not found in the target method. Its contents may have "
                        "changed since you last viewed it, or whitespace doesn't match "
                        "exactly. Current content:"
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
                        "matches exactly one location. Current content:"
                        f"`{self.target_method_signature}`:"
                        "\n\n"
                        "```java"
                        f"{content}"
                        "```"
                    ),
                })

            new_method_content = content.replace(old_str, new_str)

            file_content = path_to_workspace_copy.read_text()
            new_file_content = file_content.replace(content, new_method_content, 1)

            path_to_workspace_copy.write_text(new_file_content)

            # Now, update the mirror copy for when the agent needs to call the diff tester
            file_content = self.mirror_copy_for_differential_testing.read_text()
            new_file_content = file_content.replace(content, new_method_content, 1)
            self.mirror_copy_for_differential_testing.write_text(new_file_content)

            return json.dumps({
                "success": True,
                "message": (
                    f"Edit applied. Updated content:",
                    "\n\n"
                    f"`{self.target_method_signature}`:"
                    "```java"
                    f"{new_method_content}"
                    "```"
                )
            })

        @self.agent.tool
        def not_possible(ctx: RunContext[None], reason: str) -> str:
            """Call this if, after investigation, it becomes clear there is no way to
            fix the checker error(s) while keeping the code semantically
            equivalent to the original - i.e., any possible fix would change
            runtime behavior on some input, not just satisfy the type checker.

            Only call this tool when it is clear a semantics-preserving fix does
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
            """Call this ONLY when confident the target error is resolved. This re-runs
            the checker to verify specifically that the target error has been fixed, and
            that additional bugs were not introduced. However, other pre-existing errors
            elsewhere in the codebase do not block this from succeeding. This will also
            run the semantic-equivalence checker to ensure that all edits preserve the
            original method's behavior.

            If the error is still present, the session will NOT end and more edits must
            be made. However, `finish()` may only be called three times. If
            `finish()` is called repeatedly without actually resolving the target error,
            the session will be forcibly terminated as a failure. Only call `finish()` once
            you are confident that all edits resolves the error, does not introduce any additional
            errors, and preserves the semantics of the original method.

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

            diff_test_result, message = self.differential_tester.check_semantic_equivalence(self.diff_testing_dir)

            if diff_test_result == DifferentialTestResult.FAILURE:
                return json.dumps({
                    "finished": False,
                    "success": False,
                    "error_count": 0,
                    "errors": [],
                    "message": (
                        f"Edited method is not semantically equivalent to original: {message}"
                    ),
                })

            self.finished = True
            self.success = diff_test_result == DifferentialTestResult.SUCCESS
            self.differential_test_result = diff_test_result
            return json.dumps({
                "finished": True,
                "success": self.success,
                "error_count": 0,
                "errors": [],
                "message": "Session complete.",
            })

    def run(self) -> RefactorAgentRun:
        """
        Runs the agent on the given error.
        """
        initial_prompt = self._build_initial_prompt()

        result = RefactorAgentRun(self.orig_directory, self.modified_directory, self.differential_test_result)

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

    def _get_target_method_content(self) -> str:
        return get_method_text_for_signature(self.orig_directory / self.target_rel_path,
                                             self.target_method_signature)

    def _build_initial_prompt(self) -> str:
        return f"""You are fixing Checker Framework {self.checker_name} errors in a Java project.

## Task
The project above fails Checker Framework's {self.checker_name} Checker with 
exactly ONE error:

{self.error_to_fix.raw}

Your job is to refactor the code so that:

1. The checker passes with zero errors.
2. The refactor is semantically equivalent to the original code - it must
   preserve the exact same run-time behavior for all valid inputs. You are
   fixing a *type-checking* problem, not changing what the program does.

Semantically equivalent means, at minimum:
- No change to public method signatures' behavior as observed by callers
  (return values, thrown exceptions, side effects) for any input that was
  previously valid.
- No change to initializer values. That is, a variable or field previously
  set to a specific value (like null) should stay that value.
- No deletion of the logic that produces the warning in order to make the
  warning disappear. Removing or adding a null check, a field, a throw,
  or a code path is not a refactor, it's a behavior change.
- Prefer the smallest, most localized, semantics-preserving transformation.
  Avoid changes to unrelated code, broad refactorings, or unnecessary annotation
  changes.

If, after investigating, it is clear that there is NO refactor that both (a)
fixes the error and (b) preserves semantic equivalence - for example, the
error reflects a genuine possible null dereference that the original code
never actually guarded against, and any real fix would necessarily change
behavior on some input. Call `not_possible` with an explanation of why.

## Making code edits

You are only allowed to edit the contents of the target method, which is where
the error occurs:

`{self.target_method_signature}`:
```java
{self._get_target_method_content()}
```

There may be other parts in this method that trigger this checker. Leave them alone -
they are out of scope for this task even if it looks like an easy fix. `run_checker`
ensures you only receive errors and warnings that you need to fix.

Suppression of errors - including but not limited to `@SuppressWarnings`, assertions,
or guard clauses - are not valid fixes.

## How to work
1. For the given error, make the smallest edit that fixes it using `edit_target_method`.
   Calling `edit_target_method` with an edit MUST be the FIRST action, since you have
   already been given the error and the code to fix. If old_str is rejected as not found
   or not unique, the system will return the contents of the target method; treat that as
   fact and use it to make further edits. Recall that only code in the target method may be
   edited.
2. Call `run_checker` to verify if the edited code fixes the checker error. Ensure that no
   additional Checker Framework or compiler errors/warnings are introduced.
3. Once all errors are resolved, call `finish`. `finish` re-runs the checker and a differential tester
   to verify that the edited method 1) fixes the error, 2) does not introduce any additional errors, and
   3) is semantically-equivalent to the original. `finish` may only be called three times,
   so only call it when confident.
4. If `finish` returns with a message that the refactored method is not semantically equivalent, call
   `view_original_target_method` to review the old method definition.
"""
