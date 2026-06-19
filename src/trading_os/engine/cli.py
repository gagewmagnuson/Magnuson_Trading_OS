"""
DuckDB cross-store PIT capstone — CLI runner (V0 gate proof).

Repo path: src/trading_os/engine/cli.py

Exports the macro snapshot to a derived Parquet file, then runs the single
point-in-time query that composes the Parquet macro context with real EDGAR
fundamentals from attached Postgres, as-of one date.

    python -m trading_os.engine.cli                 # export + run, default as-of
    python -m trading_os.engine.cli --as-of 2018-12-31
    python -m trading_os.engine.cli --skip-export   # reuse existing snapshot

REMINDER: the Parquet export is a one-off V0 proof artifact, NOT an ETL pattern.
"""
from __future__ import annotations

import argparse
from datetime import date

from .config import EngineConfig
from .store import DuckDBStore


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="DuckDB cross-store PIT capstone (V0).")
    p.add_argument("--as-of", type=_parse_date, default=None,
                   help="as-of date (YYYY-MM-DD); default from config")
    p.add_argument("--skip-export", action="store_true",
                   help="reuse the existing macro Parquet snapshot")
    args = p.parse_args(argv)

    config = EngineConfig(as_of=args.as_of) if args.as_of else EngineConfig()
    as_of = config.as_of

    with DuckDBStore(config) as store:
        if not args.skip_export:
            n = store.export_macro_observations()
            print(f"[export] macro.observation -> {config.macro_silver_dir}  rows={n}")

        rows = store.capstone_world_state(as_of)
        print(f"[capstone] as-of {as_of}: {len(rows)} securities "
              f"(macro from Parquet, fundamentals from attached Postgres)")
        print(f"{'sec':>4}  {'figi':<14} {'total_assets':>20}  {'period_end':>10}  10y_yield")
        for security_id, figi, description, total_assets, period_end_date, ctx in rows:
            print(f"{security_id:>4}  {str(figi):<14} {str(total_assets):>20}  "
                  f"{str(period_end_date):>10}  {ctx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())