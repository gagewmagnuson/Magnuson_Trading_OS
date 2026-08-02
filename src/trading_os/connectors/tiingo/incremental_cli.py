"""
Daily incremental Tiingo EOD capture — forward PIT capture (DEC-024).

Repo path: src/trading_os/connectors/tiingo/incremental_cli.py
Run:
  dry-run (plan only, no fetch/write):  python -m trading_os.connectors.tiingo.incremental_cli --dry-run
  capture:                              python -m trading_os.connectors.tiingo.incremental_cli
Later, via cron:  0 22 * * 1-5  <this command>

This is the moat: everything in silver today is RECONSTRUCTED (backfilled from
current vintages, reproducible by anyone). This job CAPTURES each session forward,
recording it near real-time — history that cannot be bought back later.

Deterministic + idempotent, no heuristics:
  last_in_silver     = max session_date across the lake (parquet column stats)
  latest_completed   = most recent XNYS session whose close has passed
  target_sessions    = sessions_between(last_in_silver + 1, latest_completed)
Nothing after last_in_silver -> already up to date -> exit.

Session-at-a-time: each target session is fetched, written to its month partition,
and DQ-recorded as ITS OWN batch. A mid-catch-up failure leaves completed sessions
committed; the next run resumes deterministically (last_in_silver advanced).

Safety: aborts if last_in_silver > latest_completed (silver claims a session from
the future -> something is wrong).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

import duckdb
import psycopg

from trading_os.bars.dq import record_bar_dq
from trading_os.bars.incremental import update_monthly_partition
from trading_os.bars.knowledge_time import latest_completed_session, sessions_between
from trading_os.bars.lineage import all_seeded_symbols, close_batch, ensure_source, open_batch, resolve_security_ids
from trading_os.config import settings
from .bars import parse_bars
from .bars_cli import TIINGO_BARS_SPEC, _silver_dir
from .client import TiingoClient
from .config import TiingoConfig
from .symbols import to_tiingo_symbol


def _last_in_silver(silver_dir) -> date | None:
    """Max session_date across the whole lake glob (parquet column stats -> cheap).
    None if the lake is empty."""
    glob = f"{silver_dir.as_posix()}/*.parquet"
    con = duckdb.connect()
    try:
        import glob as _g
        if not _g.glob(glob):
            return None
        row = con.execute(f"select max(session_date) from read_parquet('{glob}')").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def run(dry_run: bool) -> int:
    config = TiingoConfig()
    silver_dir = _silver_dir(config)

    last_in_silver = _last_in_silver(silver_dir)
    latest = latest_completed_session()

    if last_in_silver is None:
        print("[incremental] silver is empty — run the historical backfill first.",
              file=sys.stderr)
        return 1
    # Safety: silver must not claim a session from the future.
    if last_in_silver > latest:
        print(f"[incremental] ABORT: last_in_silver ({last_in_silver}) is AFTER the "
              f"latest completed session ({latest}). Something is wrong.", file=sys.stderr)
        return 2

    targets = sessions_between(last_in_silver + timedelta(days=1), latest)

    # Active symbols (resolve as-of today — only active securities get new sessions).
    with psycopg.connect(settings.pg_conninfo()) as conn:
        active = all_seeded_symbols(conn)

    # --- plan preamble (operators love this) ---
    print("[incremental] === capture plan ===")
    print(f"  latest completed session : {latest}")
    print(f"  latest in silver         : {last_in_silver}")
    if not targets:
        print("  sessions to ingest       : none — up to date.")
        return 0
    print(f"  sessions to ingest ({len(targets)}):")
    for s in targets:
        print(f"    {s}")
    print(f"  active securities        : {len(active)}")
    print(f"  expected bars (max)      : {len(targets) * len(active)}")

    if dry_run:
        print("\n[incremental] dry-run — no fetch, no write.")
        return 0

    client = TiingoClient(config)
    source_spec = TIINGO_BARS_SPEC

    # --- session-at-a-time capture ---
    for session in targets:
        with psycopg.connect(settings.pg_conninfo()) as conn:
            sec_map = resolve_security_ids(conn, active)
            source_id = ensure_source(conn, source_spec)
            batch_id = open_batch(
                conn, source_id, datetime.now(timezone.utc),
                {"incremental": True, "session": session.isoformat(),
                 "active": len(sec_map)},
                code_version="tiingo-incremental-v1",
            )
            all_bars = []
            parse_anomalies = []
            fetch_failures = 0
            for symbol, sec_id in sorted(sec_map.items()):
                try:
                    rows = client.fetch_daily(to_tiingo_symbol(symbol), session, end=session)
                except Exception:  # noqa: BLE001 — one bad ticker must not kill the session
                    fetch_failures += 1
                    continue
                bars, anomalies = parse_bars(sec_id, symbol, rows)
                all_bars.extend(bars)
                parse_anomalies.extend(anomalies)

            result = update_monthly_partition(all_bars, silver_dir, "TIINGO", batch_id)
            batch_size = len(all_bars)
            record_bar_dq(conn, "bars_non_session_date", batch_id,
                          anomaly_count=len(result.skipped), batch_size=batch_size,
                          sample=[f"{s.symbol} {s.session_date} {s.reason}" for s in result.skipped])
            record_bar_dq(conn, "bars_malformed_row", batch_id,
                          anomaly_count=len(parse_anomalies), batch_size=batch_size,
                          sample=[f"{a.symbol} {a.session_date} {a.reason}" for a in parse_anomalies])
            close_batch(conn, batch_id, "succeeded",
                        rows_in=batch_size, rows_out=result.rows_written)
            conn.commit()
            print(f"[incremental] {session}: fetched {len(sec_map)} symbols "
                  f"({fetch_failures} failed), wrote {len(all_bars)} bars "
                  f"(month file now {result.rows_written} rows), "
                  f"skipped {len(result.skipped)}, parse_anomalies {len(parse_anomalies)}")

    print(f"\n[incremental] done. captured {len(targets)} session(s).")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trading_os.connectors.tiingo.incremental_cli")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no fetch/write")
    return run(dry_run=ap.parse_args(argv).dry_run)


if __name__ == "__main__":
    raise SystemExit(main())