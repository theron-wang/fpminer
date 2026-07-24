import os
import shutil
import subprocess
from pathlib import Path
from typing import Tuple

from autobuilding import enable_checkers
from utils import find_build_system_file, replace_in_uncommitted_changes

CF_URL = "https://github.com/typetools/checker-framework/"

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
    active_checker: str
    command: str

    def __init__(self, target_name: str, target_url: str):
        self.target_name = target_name
        self.target_url = target_url
        self.base_dir = Path(f"targets/{target_name}")
        build_file = find_build_system_file(self.base_dir)

        if not build_file:
            raise ValueError(f"Could not detect build file for target {target_name}")

        self.build_file = build_file

    def enable_checker(self, checker: str):
        """Enable `checker` in the build file. Should only be called once, at the start."""
        success, command = enable_checkers(
            self.target_name, self.target_url, checker, CF_URL
        )
        if not success:
            raise RuntimeError(f"Failed to enable checkers for {self.target_name}")

        assert command is not None

        self.active_checker = checker
        self.command = command

    def checkout_workspace(self, checker: str, checker_template: str) -> Tuple[Path, str]:
        """Copy the committed base repo into a fresh workspace dir for `checker`
        and return the copied repo's path and the updated checker command."""
        repo_dir = Path(f"workspace/{self.target_name}/{checker}/{self.target_name}")
        
        self.command = self.command.replace(checker_template, checker)
        self.active_checker = checker

        if os.path.exists(repo_dir):
            return repo_dir, self.command

        os.makedirs(repo_dir.parent, exist_ok=True)
        shutil.copytree(self.base_dir, repo_dir, dirs_exist_ok=True)

        replace_in_uncommitted_changes(search=checker_template, replace=checker, repo=repo_dir)
        return repo_dir, self.command

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
