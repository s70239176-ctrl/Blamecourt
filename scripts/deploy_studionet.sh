#!/usr/bin/env bash
# Minimal deploy helper. Runs the structural preflight check first --
# deploying a contract that fails it wastes a round trip to Studio for a
# class of bug that's cheap to catch locally (see scripts/preflight.py).
set -euo pipefail

CONTRACT_PATH="${1:-contracts/blamecourt.py}"

echo "Running structural preflight on ${CONTRACT_PATH}..."
python3 "$(dirname "$0")/preflight.py" "${CONTRACT_PATH}"

echo "Running money-flow / solvency tests (no GenVM needed)..."
python3 -m pytest "$(dirname "$0")/../tests/unit" -q -p no:cacheprovider

echo "Checks passed. Deploying to studionet..."
echo "(No constructor args. The appeal window is the APPEAL_WINDOW_SECONDS"
echo " constant in the contract -- it is fixed at deploy time.)"
genlayer network set studionet
genlayer deploy --contract "${CONTRACT_PATH}"
