import io
import os
import shlex
import urllib.request
import zipfile
from pathlib import Path

CHECKER_FRAMEWORK_URL = "https://github.com/typetools/checker-framework/releases/download/checker-framework-4.2.1/checker-framework-4.2.1.zip"

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

_javac_path = None
def get_command_for_checker(checker_name: str, working_dir: Path):
    global _javac_path
    if _javac_path is None:
        _javac_path = next(Path("cf").rglob("javac")).resolve()

    return f"{_javac_path} -processor {checker_name} {
        shlex.join([str(f.resolve()) for f in working_dir.glob("**/*.java")])
    }"