import os
import shutil
import subprocess
from pathlib import Path

from checker_setup.main import setup_checker
from utils import find_build_system_file, replace_in_uncommitted_changes, CheckerError

FAT_JAR_GRADLE_CONTENT = """allprojects {
    afterEvaluate { project ->
        if (project.plugins.hasPlugin("java")) {
            project.tasks.register("fpMinerFatJar", Jar) {
                archiveClassifier = "fpMiner"

                duplicatesStrategy = DuplicatesStrategy.EXCLUDE

                from {
                    project.configurations.runtimeClasspath
                            .findAll { it.name.endsWith(".jar") }
                            .collect { zipTree(it) }
                }

                from project.sourceSets.main.output
            }
        }
    }
}
"""


def _build_gradle_jar(directory: Path):
    """Build the project's fpMiner-classified fat jar via a temporary init script."""
    init_script = directory / "fpminer-fatjar.gradle"
    init_script.write_text(FAT_JAR_GRADLE_CONTENT)
    try:
        subprocess.run(
            ["./gradlew", "-I", "fpminer-fatjar.gradle", "fpMinerFatJar"],
            cwd=directory,
            check=True
        )
    finally:
        init_script.unlink(missing_ok=True)


class TargetProject:
    """Represents a target repository and its checker-specific workspaces."""
    active_checker: str
    errors: list[CheckerError]
    build_file: Path

    def __init__(self, target_name: str, target_url: str):
        """Initialize target metadata and detect its build system file."""
        self.target_name = target_name
        self.target_url = target_url
        self.base_dir = Path(f"targets/{target_name}")

    def checkout_workspace(self, checker: str, checker_template: str) -> Path:
        """
        Checks out and sets up a workspace for the given checker. Checker
        errors can be accessed through the `errors` attribute after calling this method.
        :param checker: The checker to enable.
        :param checker_template: The template to use for the checker.
        :return: The path to the workspace.
        """

        # This is ok to call on each checker. dljc is not an expensive call, and AnalysisAgent
        # only runs once per target project; subsequent calls to this will use the known checker
        # command to run the new checker.
        errors = setup_checker(
            self.target_name, self.target_url, checker
        )
        if not errors:
            raise RuntimeError(f"Failed to enable checkers for {self.target_name}")

        build_file = find_build_system_file(self.base_dir)

        if not build_file:
            raise ValueError(f"Could not detect build file for target {self.target_name}")

        self.build_file = build_file
        self.errors = errors
        self.active_checker = checker

        repo_dir = Path(f"workspace/{self.target_name}/{checker}/{self.target_name}")

        if os.path.exists(repo_dir):
            return repo_dir

        os.makedirs(repo_dir.parent, exist_ok=True)
        shutil.copytree(self.base_dir, repo_dir, dirs_exist_ok=True)

        # This is in case AnalysisAgent changes build files. However, such cases have not
        # been observed yet.
        replace_in_uncommitted_changes(search=checker_template, replace=checker, repo=repo_dir)
        return repo_dir

    def compile_jar(self) -> Path:
        """
        Compiles a fat jar for the current project. Returns the
        path to the jar. May raise if the jarring did not work
        for any reason.
        :return: The path to the fat jar
        """
        working_dir = self.build_file.parent

        if self.build_file.name == "pom.xml":
            subprocess.run(["mvn", "install", "package"], cwd=working_dir)
            candidates = list(working_dir.rglob("target/*.jar"))
        else:
            _build_gradle_jar(working_dir)
            candidates = list(working_dir.rglob("build/libs/*-fpMiner.jar"))

        if not candidates:
            raise FileNotFoundError(f"No jar found after build in {working_dir}")

        return max(candidates, key=lambda p: p.stat().st_mtime)
