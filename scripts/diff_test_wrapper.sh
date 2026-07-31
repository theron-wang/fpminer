#!/usr/bin/env bash

# Wrapper script for Differential-Test-Fuzzer
# Use this instead of Python subprocess; there is a bug (somewhere) that causes the fuzzer
# to crash with exit code 130 or hang indefinitely, likely related to some sort of output
# redirection issue. This wrapper script is a workaround for that issue.

# Usage: ./run_refactor_check.sh <target_name> <original_dir> <modified_dir>

set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "Usage: $0 <target_name> <original_dir> <modified_dir>" >&2
    exit 1
fi

TARGET_NAME="$1"
ORIGINAL_DIR="$2"
MODIFIED_DIR="$3"

PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" run.py "$TARGET_NAME" \
    --original "$ORIGINAL_DIR" \
    --refactored "$MODIFIED_DIR"