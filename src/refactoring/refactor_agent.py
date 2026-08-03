import json
import logging
import os
import shutil
from pathlib import Path

import checker_framework
from attr import dataclass
from differential_tester import DifferentialTester, DifferentialTestResult
from java.annotation_compare import are_changes_annotation_or_comment_only
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
from utils import CheckerError, run_checker_and_parse_errors, find_source_dir, whitespace_flexible_pattern

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
    orig_content: str
    modified_content: str = ""
    differential_test_result: DifferentialTestResult = DifferentialTestResult.INCONCLUSIVE
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

        if os.path.exists(self.modified_directory):
            shutil.rmtree(self.modified_directory)

        shutil.copytree(str(self.orig_directory), self.modified_directory)

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

        @self.agent.tool
        def run_checker(ctx: RunContext[None]) -> str:
            """Runs the checker analysis on the modified directory and returns a JSON
            string describing the result.

            The errors returned by this tool are the only errors you need to fix.
            `run_checker` is the trusted source of truth for what still needs work -
            if an error isn't in this list, it isn't part of your task, so there's no
            need to go looking for it elsewhere.

            As a convenience, `run_checker` also does the tedious work of filtering
            the raw checker output down to just the errors that matter for your task,
            stripping out noise you'd otherwise have to sort through by hand. It's
            always easier to just call this tool and trust what `run_checker` gives you.

            Returns a JSON object of the form:
              {
                "success": bool,
                "error_count": int,
                "errors": [
                  "Checker Framework error",
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
        def edit_target_method(ctx: RunContext[None], old_str: str, new_str: str) -> str:
            """Replace an exact block of text in the target method with new text.

            old_str must match a part of the target method's raw content EXACTLY and must appear
            exactly once in the target method. If old_str appears zero times or more than once,
            the edit is rejected and no changes are made. Then, widen old_str with a bit more
            surrounding context (i.e., an extra line above/below) until old_str is unique.

            Returns a JSON string:
              {"success": bool, "message": str}

            A call to `edit_target_method`, regardless of whether the edit was successful,
            will return the full content of the updated target method.
            """
            GLOBAL_MODEL_RATE_LIMITER.acquire()

            path_to_workspace_copy = Path(self.error_to_fix.file_path)

            content = self._get_current_target_method_content()
            pattern = whitespace_flexible_pattern(old_str)
            matches = list(pattern.finditer(content))

            count = len(matches)

            if count == 0:
                return json.dumps({
                    "success": False,
                    "message": (
                        "old_str not found in the target method. Its contents may have "
                        "changed since you last viewed it, or whitespace doesn't match "
                        "exactly. Current content:"
                        "\n\n"
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
                        "\n\n"
                        "```java"
                        f"{content}"
                        "```"
                    ),
                })

            match = matches[0]
            new_method_content = content[:match.start()] + new_str + content[match.end():]

            file_content = path_to_workspace_copy.read_text()
            new_file_content = file_content.replace(content, new_method_content, 1)

            path_to_workspace_copy.write_text(new_file_content)

            # Now, update the mirror copy for when the agent needs to call the diff tester
            file_content = self.mirror_copy_for_differential_testing.read_text()

            # Formatting may be different in the diff test copy (since Specimin runs a formatter)
            content = get_method_text_for_signature(self.mirror_copy_for_differential_testing,
                                                    self.target_method_signature)
            new_file_content = file_content.replace(content, new_method_content, 1)
            self.mirror_copy_for_differential_testing.write_text(new_file_content)

            return json.dumps({
                "success": True,
                "message": (
                    f"Edit applied. Updated content:",
                    "\n\n"
                    "```java"
                    f"{new_method_content}"
                    "```"
                )
            })

        @self.agent.tool
        def not_possible(ctx: RunContext[None], reason: str) -> str:
            """Call `not_possible` if, after investigation, you are confident you cannot
            fix the checker error(s) while keeping the code semantically
            equivalent to the original - i.e., any possible fix would change
            runtime behavior on some input, not just satisfy the type checker.

            Only call `not_possible` tool when you are confident a semantics-preserving fix does
            not exist. `not_possible` ends the session immediately; no further tool calls
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
            """Call `finish` ONLY when confident the target error is resolved. `finish`
            re-runs the checker, using `run_checker`, to verify specifically that the
            target error has been fixed, and that additional bugs were not introduced.
            `finish` will also run a semantic-equivalence checker to ensure that all
            edits preserve the original method's behavior.

            If the error is still present, the session will NOT end and you must make more edits.
            However, `finish` may only be called three times. If `finish` is called repeatedly
            without actually resolving the target error, the session will be forcibly terminated as
            a failure. Only call `finish` once you are confident that all edits resolves the error,
            does not introduce any additional errors, and preserves the semantics of the original method.

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
                            "Maximum finish attempts exceeded. The session is now "
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
                        "edits with `edit_target_method`, and call finish again once "
                        "resolved."
                    ),
                })

            if are_changes_annotation_or_comment_only(self._get_original_target_method_content(),
                                                      self._get_current_target_method_content()):
                # If two methods only differ in annotations or comments, they are semantically
                # equivalent, always. Differential-Test-Fuzzer only skips cases where the
                # _text_ of two methods is exactly equivalent, so we can bail out on cases
                # we know will yield SUCCESS and save a minute.
                diff_test_result = DifferentialTestResult.SUCCESS
                message = ""
            else:
                diff_test_result, message = self.differential_tester.check_semantic_equivalence(self.diff_testing_dir)

            if diff_test_result == DifferentialTestResult.FAILURE:
                return json.dumps({
                    "finished": False,
                    "success": False,
                    "error_count": 0,
                    "errors": [],
                    "message": (
                        f"Edited method is not semantically equivalent to original. Reason: {message}"
                        "\n\nOriginal method:\n"
                        "```java\n"
                        f"{self._get_original_target_method_content()}"
                        "\n```"
                        "\n\nEdited method:\n"
                        "```java\n"
                        f"{self._get_current_target_method_content()}"
                        "\n```\n"
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

        result = RefactorAgentRun(self._get_original_target_method_content())

        try:
            self._run_sync_with_rate_limit_retry(initial_prompt)
        except Exception as e:
            result.modified_content = self._get_original_target_method_content()
            result.error = f"Agent run failed or exhausted limits: {e}"
            return result

        result.modified_content = self._get_original_target_method_content()
        result.differential_test_result = self.differential_test_result

        # agent.run() returned normally, but that doesn't guarantee finish
        # or not_possible() was ever actually called - the model may have
        # just stopped producing tool calls.
        if not self.finished:
            result.error = (
                "Agent stopped without calling finish or not_possible()."
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

    def _get_original_target_method_content(self) -> str:
        return get_method_text_for_signature(self.orig_directory / self.target_rel_path,
                                             self.target_method_signature)

    def _get_current_target_method_content(self) -> str:
        return get_method_text_for_signature(self.modified_directory / self.target_rel_path,
                                             self.target_method_signature)

    def _build_initial_prompt(self) -> str:
        return f"""You are fixing Checker Framework {self.checker_name} errors in a Java project.

## Task
The project above fails Checker Framework's {self.checker_name} Checker with 
exactly ONE error:

{self.error_to_fix.raw}

Your job is to refactor the code so that:

1. The checker passes with zero errors.
2. The refactor is semantically equivalent to the original code - the refactor
   must preserve the exact same run-time behavior for all valid inputs. You are
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

If, after investigating, you are confident that there is NO refactor that
both (a) fixes the error and (b) is semantically equivalent to the original code -
for example, the error reflects a genuine possible null dereference that the
original code never actually guarded against, and any real fix would necessarily change
behavior on some input. Call `not_possible` with an explanation of why.

## Making code edits

You are only allowed to edit the contents of the target method, which is where
the error occurs:

```java
{self._get_original_target_method_content()}
```

## Determining the correct action

You should classify the warning into exactly one of two cases:

Case 1: False positive; the checker is wrong. In this case:
- Make a semantics-preserving refactoring that changes the code so the checker can itself verify
  the property. For example: restructuring control flow so the checker's flow-sensitive analysis
  can follow the invariant, extracting a local variable to stabilize a field read,
  adding a checker-recognized annotation that accurately describes the true contract
  (not just silencing). The change must not alter observable behavior for any input.
- A true refactor is a change that improves the code's structure or design without changing its behavior.
  A warning suppression, by contrast, only makes the warning disappear while that same input still triggers
  the exception - the warning is silenced, but the underlying problem persists untouched. This holds
  regardless of the mechanism used to silence the warning, whether @SuppressWarnings, an assertion, a
  dynamic null-check like Objects.nonNull(), or anything else with the same effect.
  
Case 2: Genuine bug. The checker is correct: there is a real input or code path under which
the flagged expression can be null (or otherwise violate the checker's guarantee) at runtime.
In this case, call `not_possible`. A genuine bug is out of scope for this task; your job is to
resolve false positives, not to patch real defects (patching a real defect usually requires
a behavioral change, which risks breaking semantics, and is a separate task from what you're
being asked to do here).

Start under the assumption that the warnings are under case 1. If you are confident the
warning reveals a genuine bug, or become confident that the warning represents a real bug
once you have tried different edits, you should consider case 2 and call `not_possible` with
a precise technical explanation of why no semantics-preserving fix exists.

## How to work
1. For the given error, make the smallest edit that fixes the error using `edit_target_method`.
   Calling `edit_target_method` with an edit MUST be the FIRST action, since you have
   already been given the error and the code to fix. If old_str is rejected as not found
   or not unique, the system will return the contents of the target method; treat that as
   fact and use those contents to make further edits. Recall that only code in the target
   method may be edited.
2. Call `run_checker` to verify if the edited code fixes the checker error. Ensure that no
   additional Checker Framework or compiler errors/warnings are introduced. A call to `run_checker`
   provides all the errors and warnings you must fix in order to pass validation, while conveniently
   filtering out any errors that you do not need to worry about.
3. Once all errors are resolved, call `finish`. `finish` re-runs the checker and a differential tester
   to verify that the edited method (1) fixes the error, (2) does not introduce any additional errors, and
   (3) is semantically equivalent to the original. `finish` may only be called three times,
   so only call `finish` when confident.
"""
