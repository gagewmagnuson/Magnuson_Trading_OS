#!/bin/bash
# Weekly PIT capture for macro (FRED) + fundamentals (EDGAR) — launchd wrapper.
set -euo pipefail

REPO="/Users/gagemagnuson/Magnuson_Trading_OS"
PYTHON="$REPO/.venv/bin/python"

cd "$REPO"
export PYTHONPATH="$REPO/src"

echo "===== weekly capture $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

echo "--- FRED (macro vintages) ---"
"$PYTHON" -m trading_os.connectors.fred.cli
fred_rc=$?
echo "--- FRED exit code $fred_rc ---"

echo "--- EDGAR (fundamentals) ---"
"$PYTHON" -m trading_os.connectors.edgar.cli
edgar_rc=$?
echo "--- EDGAR exit code $edgar_rc ---"

echo "===== weekly capture done (fred=$fred_rc edgar=$edgar_rc) ====="