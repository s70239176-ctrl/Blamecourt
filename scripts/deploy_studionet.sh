#!/usr/bin/env bash
# Minimal deploy helper. Runs the structural preflight check first --
# deploying a contract that fails it wastes a round trip to Studio for a
# class of bug that's cheap to catch locally (see scripts/preflight.py).
set -euo pipefail

CONTRACT_PATH="${1:-contracts/blamecourt.py}"

echo "Running structural preflight on ${CONTRACT_PATH}..."
python3 "$(dirname "$0")/preflight.py" "${CONTRACT_PATH}"

echo "Preflight passed. Deploying to studionet..."
genlayer network set studionet
genlayer deploy --contract "${CONTRACT_PATH}"
