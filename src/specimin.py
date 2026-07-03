import os
import subprocess

specimin = "specimin"
specimin_url = "https://github.com/njit-jerse/specimin.git"
specimin_branch = "main"

def setup():
    if os.path.exists(specimin):
        print("Specimin already exists: pulling most recent changes")
        subprocess.run(
            ["git", "-C", specimin, "pull"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    print("Cloning Specimin from GitHub")
    subprocess.run(
        ["git", "clone", specimin_url, specimin, "-b", specimin_branch, "--depth", "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )