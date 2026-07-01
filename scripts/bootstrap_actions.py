"""
Thin wrapper around connectors.tiingo.loader.bootstrap — the canonical path for
seeding corp.corporate_action from Tiingo during the bootstrap phase.

The implementation lives in the connector package (loader.py); this script only
parses args and invokes it, so when the production Tiingo connector is built the
write logic doesn't move.

Usage (repo root, venv, Tiingo key in .env, Postgres reachable):
    PYTHONPATH=src python scripts/bootstrap_actions.py --symbols AAPL NVDA TSLA
    PYTHONPATH=src python scripts/bootstrap_actions.py --symbols AAPL --start 2016-01-01
"""
from __future__ import annotations

import argparse
from datetime import date

from trading_os.connectors.tiingo.loader import bootstrap


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Bootstrap-load Tiingo corporate actions.")
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=None)
    args = p.parse_args(argv)

    results = bootstrap(args.symbols, args.start, args.end)

    print("=" * 60)
    print("BOOTSTRAP CORPORATE ACTIONS (Tiingo -> corp.corporate_action)")
    print("=" * 60)
    total_ins = total_skip = total_conf = 0
    for st in results:
        print(f"\n{st.symbol}: fetched {st.fetched} | inserted {st.inserted} | "
              f"skipped(exact) {st.skipped_exact} | conflicts {st.conflicts}")
        for w in st.warnings:
            print(f"   ! {w}")
        total_ins += st.inserted
        total_skip += st.skipped_exact
        total_conf += st.conflicts
    print("-" * 60)
    print(f"TOTAL: inserted {total_ins}, skipped(exact) {total_skip}, "
          f"conflicts {total_conf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())