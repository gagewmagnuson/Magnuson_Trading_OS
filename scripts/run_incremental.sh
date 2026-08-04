#!/bin/bash
# Daily incremental Tiingo capture — launchd wrapper.
set -euo pipefail

REPO="/Users/gagemagnuson/Magnuson_Trading_OS"
PYTHON="$REPO/.venv/bin/python"

cd "$REPO"
export PYTHONPATH="$REPO/src"

echo "===== nightly run $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

echo "--- stage 1: EOD bars capture ---"
"$PYTHON" -m trading_os.connectors.tiingo.incremental_cli
bars_rc=$?
echo "--- stage 1 exit code $bars_rc ---"

echo "--- stage 2: Gold refresh (open partitions) ---"
"$PYTHON" -m trading_os.gold.refresh --nightly
gold_rc=$?
echo "--- stage 2 exit code $gold_rc ---"

echo "===== nightly run done (bars=$bars_rc gold=$gold_rc) ====="
