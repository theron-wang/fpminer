import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

GRADLE_JAR_INIT_SCRIPT_PATH = Path("scripts") / "fpminer-fatjar.gradle"


def build_gradle_jar(directory: Path) -> list[Path]:
    """Build the project's fpMiner-classified fat jar via an init script."""
    subprocess.run(
        ["./gradlew", "-I", GRADLE_JAR_INIT_SCRIPT_PATH, "fpMinerFatJar"],
        cwd=directory,
        check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return list(directory.rglob("target/*.jar"))


def build_maven_jar(pom_xml_path: Path) -> list[Path]:
    """Build a single fpMiner-classified fat jar containing every jar-packaged
    module reachable (recursively) from pom_xml_path, mirroring the Gradle
    task's sweep over allprojects with the java plugin."""
    project_dir = pom_xml_path.parent

    subprocess.run(
        ["./mvnw", "install", "-Dmaven.test.skip=true"],
        cwd=project_dir,
        check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    gavs = _discover_jar_modules(pom_xml_path)
    if not gavs:
        raise RuntimeError(f"No jar-packaged modules found under {pom_xml_path}")

    synthetic_pom = project_dir / "pom-fpMiner.xml"
    _write_fatjar_pom(synthetic_pom, gavs)

    try:
        subprocess.run(
            ["./mvnw", "-f", synthetic_pom.name, "package"],
            cwd=project_dir,
            check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return list(project_dir.rglob("target/*-fpMiner.jar"))
    finally:
        try:
            os.unlink(str(synthetic_pom))
        except KeyboardInterrupt:
            raise
        except Exception:
            pass


FATJAR_POM_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.fpminer.fatjar</groupId>
    <artifactId>fpminer-fatjar</artifactId>
    <version>1.0.0</version>
    <packaging>jar</packaging>

    <dependencies>
{dependencies}
    </dependencies>

    <build>
        <plugins>
            <plugin>
                <groupId>org.apache.maven.plugins</groupId>
                <artifactId>maven-shade-plugin</artifactId>
                <version>3.6.2</version>
                <executions>
                    <execution>
                        <phase>package</phase>
                        <goals>
                            <goal>shade</goal>
                        </goals>
                        <configuration>
                            <createDependencyReducedPom>false</createDependencyReducedPom>
                            <shadedArtifactAttached>true</shadedArtifactAttached>
                            <shadedClassifierName>fpMiner</shadedClassifierName>
                        </configuration>
                    </execution>
                </executions>
            </plugin>
        </plugins>
    </build>
</project>
"""

DEPENDENCY_TEMPLATE = """        <dependency>
            <groupId>{group_id}</groupId>
            <artifactId>{artifact_id}</artifactId>
            <version>{version}</version>
        </dependency>"""


def _write_fatjar_pom(pom_path: Path, gavs: list[tuple[str, str, str]]) -> None:
    dependencies_xml = "\n".join(
        DEPENDENCY_TEMPLATE.format(group_id=g, artifact_id=a, version=v)
        for g, a, v in gavs
    )
    pom_path.write_text(
        FATJAR_POM_TEMPLATE.format(dependencies=dependencies_xml),
        encoding="utf-8",
    )


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _child_text(elem: ET.Element, name: str) -> str | None:
    for child in elem:
        if _strip_ns(child.tag) == name:
            return child.text
    return None


def _resolve_gav(pom_path: Path) -> tuple[str | None, str | None, str | None, str]:
    """Returns (groupId, artifactId, version, packaging), following <parent>
    inheritance on disk when a field isn't declared locally."""
    root = ET.parse(pom_path).getroot()

    group_id = _child_text(root, "groupId")
    artifact_id = _child_text(root, "artifactId")
    version = _child_text(root, "version")
    packaging = _child_text(root, "packaging") or "jar"

    parent_elem = next((c for c in root if _strip_ns(c.tag) == "parent"), None)
    if parent_elem is not None and (group_id is None or version is None):
        group_id = group_id or _child_text(parent_elem, "groupId")
        version = version or _child_text(parent_elem, "version")

        relative_path = _child_text(parent_elem, "relativePath")
        if relative_path is None:
            relative_path = "../pom.xml"
        if relative_path:
            parent_pom = (pom_path.parent / relative_path).resolve()
            if parent_pom.is_file() and (group_id is None or version is None):
                p_group, _, p_version, _ = _resolve_gav(parent_pom)
                group_id = group_id or p_group
                version = version or p_version

    return group_id, artifact_id, version, packaging


def _discover_jar_modules(
        pom_path: Path, seen: set[Path] | None = None
) -> list[tuple[str, str, str]]:
    """Recursively walk <module> entries from pom_path, returning
    (groupId, artifactId, version) for every jar-packaged module found,
    at any nesting depth."""
    if seen is None:
        seen = set()

    pom_path = pom_path.resolve()
    if pom_path in seen:
        return []
    seen.add(pom_path)

    root = ET.parse(pom_path).getroot()
    modules_elem = next((c for c in root if _strip_ns(c.tag) == "modules"), None)
    module_names = (
        [m.text for m in modules_elem if _strip_ns(m.tag) == "module" and m.text]
        if modules_elem is not None
        else []
    )

    gavs: list[tuple[str, str, str]] = []

    for name in module_names:
        child_pom = pom_path.parent / name / "pom.xml"
        if child_pom.is_file():
            gavs.extend(_discover_jar_modules(child_pom, seen))

    group_id, artifact_id, version, packaging = _resolve_gav(pom_path)
    if packaging == "jar" and group_id and artifact_id and version:
        gavs.append((group_id, artifact_id, version))

    return gavs
