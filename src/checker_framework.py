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


def _download(url: str):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def setup():
    if os.path.exists("cf"):
        return

    print("Downloading the Checker Framework.")

    zip_bytes = _download(CHECKER_FRAMEWORK_URL)

    os.makedirs("cf", exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall("cf")

    print("Successfully downloaded the Checker Framework.")


def get_path_to_dljc() -> Path:
    # CF ships with dljc
    return next(Path("cf").rglob("dljc"))


def get_path_to_checker_dir() -> Path:
    return next(Path("cf").iterdir())


def get_path_to_checker_jar() -> Path:
    return next(Path("cf").rglob("checker.jar"))


_javac_path = None


def get_javac_path():
    global _javac_path
    if _javac_path is None:
        _javac_path = next(Path("cf").rglob("javac")).resolve()
    return _javac_path


def get_command_for_checker(checker_name: str, working_dir: Path):
    return f"{get_javac_path()} -processor {checker_name} {
    shlex.join([str(f.resolve()) for f in working_dir.glob("**/*.java")])
    }"
