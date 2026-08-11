import io
import os
import shlex
import urllib.request
import zipfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

CHECKER_FRAMEWORK_VERSION = os.getenv("CHECKER_FRAMEWORK_VERSION", "4.2.1")
CHECKER_FRAMEWORK_URL = (
    f"https://github.com/typetools/checker-framework/releases/download/"
    f"checker-framework-{CHECKER_FRAMEWORK_VERSION}/checker-framework-{CHECKER_FRAMEWORK_VERSION}.zip"
)
DOWNLOAD_TO = Path("tools/cf").resolve()


def _download(url: str):
    """Download raw bytes from the provided URL."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def setup():
    """Ensure the Checker Framework is downloaded under `tools/cf`."""
    if os.path.exists(DOWNLOAD_TO):
        return

    zip_bytes = _download(CHECKER_FRAMEWORK_URL)

    os.makedirs(DOWNLOAD_TO, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(DOWNLOAD_TO)


def get_path_to_dljc() -> Path:
    """Return the path to the bundled `dljc` executable."""
    # CF ships with dljc
    return next(Path(DOWNLOAD_TO).rglob("dljc"))


def get_path_to_checker_dir() -> Path:
    """Return the extracted Checker Framework root directory."""
    return next(Path(DOWNLOAD_TO).iterdir())


def get_path_to_checker_jar() -> Path:
    """Return the path to `checker.jar` from the local CF install."""
    return next(Path(DOWNLOAD_TO).rglob("checker.jar"))


_javac_path = None


def get_javac_path():
    """Resolve and cache the Checker Framework `javac` path."""
    global _javac_path
    if _javac_path is None:
        _javac_path = next(Path(DOWNLOAD_TO).rglob("javac")).resolve()
    return _javac_path


def get_command_for_checker(checker_name: str, working_dir: Path):
    """Build a shell command that runs a checker on all Java files in `working_dir`."""
    return f"{get_javac_path()} -processor {checker_name} {
    shlex.join([str(f.resolve()) for f in working_dir.glob("**/*.java")])
    }"
