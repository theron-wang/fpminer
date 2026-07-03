from __future__ import annotations

import argparse
import os
import json
import specimin
import autobuilding

from dataclasses import dataclass

checker_framework_url = "https://github.com/typetools/checker-framework/"

def run(target: Target, checkers: list[str]):
    # use the first checker; we will modify the resulting dockerfile for the other checkers
    autobuilding.enable_checkers(target.name, target.url, checkers[0], checker_framework_url)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--checkers", type=str, help="Path to the file containing the list of checkers to run")
    parser.add_argument("-t", "--targets", type=str, help="Path to the file containing the list of repositories to run on")

    args = parser.parse_args()

    if args.checkers is None:
        print("Error: Please provide a path to the file containing the list of checkers to run using the -c or --checkers option.")
        exit(1)

    if not os.path.exists(args.checkers):
        print("Error: The specified checkers file does not exist.")
        exit(1)

    if args.targets is None:
        print("Error: Please provide a path to the file containing the list of repositories to run on using the -t or --targets option.")
        exit(1)

    if not os.path.exists(args.targets):
        print("Error: The specified targets file does not exist.")
        exit(1)

    with open(args.checkers) as f:
        checkers = [line.strip() for line in f.readlines()]

    with open(args.targets) as f:
        targets = [Target(**json.loads(line)) for line in f]

    specimin.setup()

    for target in targets:
        run(target, checkers)

@dataclass
class Target:
    name: str
    url: str

if __name__ == "__main__":
    main()