import os
import subprocess
import xml.etree.ElementTree as XML
from enum import Enum
from pathlib import Path
from typing import Tuple

import regex
from utils import run_git_reset_hard

DIFF_TEST_REPO_URL = "https://github.com/musta55/Differential-Fuzz-Testing.git"
DIFF_TEST_DIR = "tools/diff_test"
DIFF_TEST_WRAPPER = Path("scripts/diff_test_wrapper.sh").resolve()

POM_NS = "http://maven.apache.org/POM/4.0.0"


def setup():
    """Ensure the differential-testing helper repository is available locally."""
    if os.path.exists(DIFF_TEST_DIR):
        # Reset first in case diff test directory changes were not cleaned up properly
        run_git_reset_hard(Path(DIFF_TEST_DIR))

        subprocess.run(
            ["git", "pull"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=DIFF_TEST_DIR)
        return
    subprocess.run(
        ["git", "clone", DIFF_TEST_REPO_URL, DIFF_TEST_DIR, "--depth", "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )


def read_fuzz_report(path: Path) -> list[Tuple[str, str]]:
    """
    Reads the differential tester's markdown report, and extracts the methods' test
    result and reason for divergence. Returns a list of tuples of (result, evidence).

    :param path: The path to the fuzz report
    :return: A list of tuples of (result, evidence)
    """
    text = path.read_text()

    reports = []

    # Reports look like this:
    """
    | Method | Result | Tests (fail) | Branch | Line | Evidence |
    |--------|--------|--------------|--------|------|----------|
    | Simple.foo | **DIVERGENT** | 34 (34) | 0/0 | 1/1 | orig: returns null vs ref: throws Error |
    | Simple.bar | **DIVERGENT** | 5 (5) | 0/0 | 1/1 | orig: returns null vs ref: throws Error |
    """

    result_index = 1
    evidence_index = 5

    for line in text.split("\n"):
        if not line.startswith("|") or line.startswith("|-"):
            continue

        parts = [part.strip() for part in line.split("|") if part.strip()]

        # In case the implementation changes down the line
        if "Result" in parts:
            result_index = parts.index("Result")
            evidence_index = parts.index("Evidence")
            continue

        reports.append((parts[result_index].replace("*", ""), parts[evidence_index]))

    return reports


def _format_maven_profile(jar_path: Path, target_name: str) -> str:
    """
    Format a Maven profile string for the given jar path and target name.
    """
    return f"""<profile>
  <id>{target_name}</id>
  <dependencies>
    <dependency>
      <groupId>fpminer</groupId>
      <artifactId>fpminer-uber-jar</artifactId>
      <version>1.0.0</version>
      <scope>system</scope>
      <systemPath>{jar_path}</systemPath>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.codehaus.mojo</groupId>
        <artifactId>build-helper-maven-plugin</artifactId>
        <version>3.2.0</version>
        <executions><execution>
          <id>add-{target_name}</id>
          <phase>generate-test-sources</phase>
          <goals><goal>add-test-source</goal></goals>
          <configuration><sources>
            <source>src/test/Dataset/{target_name}</source>
            <source>src/test/fuzzing/{target_name}</source>
          </sources></configuration>
        </execution></executions>
      </plugin>
    </plugins>
  </build>
</profile>
"""


class DifferentialTestResult(Enum):
    EQUIVALENT = 0
    NOT_EQUIVALENT = 1
    INCONCLUSIVE = 2


class DifferentialTester:
    """Runs differential fuzz testing between original and refactored project code."""

    def __init__(self, jar_path: Path, original_dir: Path, target_name: str):
        """
        Configure the differential tester to evaluate one target project jar.

        :param jar_path: Path to the built target project jar.
        :param original_dir: Path to the original project directory.
        :param target_name: Identifier of the target project dataset/profile.
        """
        self.target_name = target_name
        self.original_dir = original_dir

        # Reset first in case diff test directory changes were not cleaned up properly
        run_git_reset_hard(Path(DIFF_TEST_DIR))

        # Replace the profile in pom.xml to this project
        XML.register_namespace("", POM_NS)
        ns = {"m": POM_NS}

        pom_path = Path(DIFF_TEST_DIR) / "pom.xml"
        tree = XML.parse(pom_path)
        root = tree.getroot()

        # Wrap the single profile string in a namespaced <profiles> so it parses
        # with the correct namespace instead of picking up a blank one.
        profile_str = _format_maven_profile(jar_path.resolve(), target_name)
        new_profiles = XML.fromstring(f'<profiles xmlns="{POM_NS}">{profile_str}</profiles>')

        old_profiles = root.find("m:profiles", ns)
        if old_profiles is not None:
            idx = list(root).index(old_profiles)
            root.remove(old_profiles)
            root.insert(idx, new_profiles)
        else:
            root.append(new_profiles)

        tree.write(pom_path, encoding="utf-8", xml_declaration=True)

    def check_semantic_equivalence(self, modified_dir: Path) -> Tuple[DifferentialTestResult, str]:
        """
        Runs the differential test on the original directory and the modified directory.
        Returns the result of the test and the error message, if applicable. This method
        may return INCONCLUSIVE if some error makes the differential tester never run.

        :param modified_dir: The directory to test against (the "refactored")
        :return: The result of the test, along with an error message if applicable
        """

        # We use a bash script around the differential test fuzzer because something with
        # output redirection, if we call its script directly, causes the subprocess to crash
        # with exit code 130 or hang indefinitely.
        proc = subprocess.run(
            [str(DIFF_TEST_WRAPPER), self.target_name, str(self.original_dir.resolve()),
             str(modified_dir.resolve())],
            cwd=DIFF_TEST_DIR,
            capture_output=True,
            text=True,
            start_new_session=True
        )

        if proc.returncode != 0:
            return DifferentialTestResult.INCONCLUSIVE, ""

        if "EQUIVALENT=1" in proc.stdout:
            return DifferentialTestResult.EQUIVALENT, ""

        fuzz_report_path = Path(regex.match(r"->\s*(?P<report>.*)", proc.stdout).group("report"))

        result = read_fuzz_report(fuzz_report_path)

        if len(result) > 1:
            return DifferentialTestResult.INCONCLUSIVE, "More than one method was modified when only one should have been"
        elif len(result) == 0:
            # This is technically successful, since this means all methods are the same between the two.
            # Since this is used by refactor_agent anyway, the checker will not pass if no changes have
            # been made, so this isn't a problem (or this is an annotation-only change, and it will be
            # handled elsewhere anyway)
            return DifferentialTestResult.EQUIVALENT, ""

        status, reasoning = result[0]

        if status == "DIVERGENT":
            return DifferentialTestResult.NOT_EQUIVALENT, reasoning
        else:
            return DifferentialTestResult.INCONCLUSIVE, ""
