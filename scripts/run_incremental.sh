#!/bin/bash
# Daily incremental Tiingo capture — launchd wrapper.
set -euo pipefail

REPO="/Users/gagemagnuson/Magnuson_Trading_OS"
PYTHON="$REPO/.venv/bin/python"

cd "$REPO"
export PYTHONPATH="$REPO/src"

echo "===== incremental run $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
"$PYTHON" -m trading_os.connectors.tiingo.incremental_cli
echo "===== exit code $? ====="
