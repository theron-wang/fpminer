import os
import subprocess
import xml.etree.ElementTree as XML
from pathlib import Path

from utils import run_git_reset_hard

DIFF_TEST_REPO_URL = "https://github.com/musta55/Differential-Fuzz-Testing.git"
DIFF_TEST_DIR = "diff_test"

POM_NS = "http://maven.apache.org/POM/4.0.0"


def setup():
    """Ensure the differential-testing helper repository is available locally."""
    if os.path.exists(DIFF_TEST_DIR):
        # Reset first in case diff test directory changes were not cleaned up properly
        run_git_reset_hard(Path(DIFF_TEST_DIR))

        print("Differential tester already exists: pulling most recent changes")
        subprocess.run(
            ["git", "pull"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=DIFF_TEST_DIR)
        return
    print("Cloning Differential-Fuzz-Testing from GitHub")
    subprocess.run(
        ["git", "clone", DIFF_TEST_REPO_URL, DIFF_TEST_DIR, "--depth", "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )


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

    def check_semantic_equivalence(self, modified_dir: Path) -> bool:
        """
        Run differential tests against the refactored code.

        :param modified_dir: Path to the refactored project directory.
        :return: True when the run reports no divergence, otherwise False.
        """
        result = subprocess.run(
            ["python3", "run.py", self.target_name, "--original", str(self.original_dir.resolve()), "--refactored",
             str(modified_dir.resolve())],
            cwd=DIFF_TEST_DIR,
            capture_output=True,
            text=True
        )

        # EQUIVALENT=1 cannot be checked since we run the differential tester on the original
        # project file, so there may be other methods beside the target method

        # The other methods haven't changed, so they will never count towards DIVERGENT (but may
        # towards ERROR or SKIP), so if DIVERGENT=0, then the method is (likely) semantically equivalent.
        return result.returncode == 0 and not "DIVERGENT=0" in result.stdout
