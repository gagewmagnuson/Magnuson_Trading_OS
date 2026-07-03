"""
Tiingo corporate-actions connector CLI. Canonically ingests splits + cash
dividends for the seeded universe into corp.corporate_action (source TIINGO),
with meta.ingest_batch lineage. Mirrors the Alpaca bars CLI.

Design points:
  * Targets = seeded securities (resolve-and-skip, DEC-017). Default: full
    universe; --symbols / --limit narrow it.
  * knowledge_time = ingest fetch time (DEC-018), one timestamp per run.
  * RESUMABLE: each symbol commits independently, so if Tiingo's free-tier daily
    cap is hit mid-run, everything fetched so far is persisted and a later run
    resumes with the remaining symbols (already-written actions exact-skip).
  * --incremental pulls only a recent window per symbol (cheap steady-state
    refresh); full history otherwise.

Usage (repo root, venv, Tiingo key in .env, Postgres reachable):
    python -m trading_os.connectors.tiingo.cli --symbols AAPL NVDA TSLA
    python -m trading_os.connectors.tiingo.cli --limit 20
    python -m trading_os.connectors.tiingo.cli                    # full universe
    python -m trading_os.connectors.tiingo.cli --incremental      # recent-window refresh
    python -m trading_os.connectors.tiingo.cli --symbols AAPL --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone

import psycopg

from trading_os.config import settings

from .actions import derive_actions
from .client import TiingoClient
from .config import HISTORY_START, INCREMENTAL_WINDOW_DAYS, TiingoConfig
from .writer import ActionsWriter, BatchStats


def _all_seeded_symbols(conn, limit: int | None) -> list[str]:
    rows = conn.execute(
        """
        select si.id_value
        from sec.security_identifier si
        where si.id_type = 'TICKER'
          and si.valid_from <= current_date
          and (si.valid_to is null or si.valid_to >= current_date)
        order by si.id_value
        """
    ).fetchall()
    syms = [r[0] for r in rows]
    return syms[:limit] if limit else syms


def run(conn, config: TiingoConfig, client: TiingoClient, requested: list[str],
        start: date, dry_run: bool) -> int:
    writer = ActionsWriter(conn, config)
    sec_map = writer.resolve_security_ids(requested)
    skipped = [s for s in requested if s not in sec_map]
    print(f"[tiingo] {len(requested)} symbols requested, {len(sec_map)} resolved, "
          f"{len(skipped)} skipped"
          + (f" e.g. {', '.join(skipped[:8])}" if skipped else ""))
    if not sec_map:
        print("[tiingo] nothing to ingest (no requested symbol is in the master).")
        return 0

    kt = datetime.now(timezone.utc)             # DEC-018: ingest time, one per run
    stats = BatchStats()
    source_id = writer.ensure_source()
    batch_id = None
    if not dry_run:
        batch_id = writer.open_batch(source_id, kt, {"start": start.isoformat(),
                                                     "symbols": len(sec_map)})
        conn.commit()

    t0 = time.time()
    try:
        for sym, sid in sec_map.items():
            try:
                rows = client.fetch_daily(sym, start)
                stats.api_calls += 1
                actions, warns = derive_actions(sid, rows)
                stats.warnings.extend(f"{sym}: {w}" for w in warns)
                if dry_run:
                    n_s = sum(1 for a in actions if a.action_type == "SPLIT")
                    n_d = sum(1 for a in actions if a.action_type == "CASH_DIVIDEND")
                    print(f"  {sym}: {len(actions)} actions ({n_s} splits, {n_d} divs) [dry-run]")
                    stats.symbols_processed += 1
                    continue
                res = writer.write_symbol_actions(actions, kt, sym)
                stats.inserted += res.inserted
                stats.skipped_exact += res.skipped_exact
                stats.conflicts += res.conflicts
                stats.warnings.extend(res.warnings)
                conn.commit()                    # per-symbol commit -> resumable
                stats.symbols_processed += 1
                print(f"  {sym}: inserted {res.inserted}, skipped {res.skipped_exact}, "
                      f"conflicts {res.conflicts}")
            except Exception as e:               # noqa: BLE001 (one symbol must not kill the run)
                conn.rollback()
                stats.symbols_failed += 1
                stats.failures.append(f"{sym}: {e}")
                print(f"  {sym}: FAILED — {e}", file=sys.stderr)
            time.sleep(config.request_interval)
    finally:
        stats.elapsed_seconds = time.time() - t0

    if dry_run:
        print(f"[tiingo] dry-run: {stats.symbols_processed} symbols, no writes.")
        return 0

    status = "succeeded" if stats.symbols_failed == 0 else "partial"
    params = stats.as_params({"start": start.isoformat(), "symbols": len(sec_map)})
    writer.close_batch(batch_id, status,
                       rows_in=stats.inserted + stats.skipped_exact + stats.conflicts,
                       rows_out=stats.inserted, params=params)
    conn.commit()
    print(f"[tiingo] batch {batch_id} {status}: inserted {stats.inserted}, "
          f"skipped {stats.skipped_exact}, conflicts {stats.conflicts}, "
          f"failed {stats.symbols_failed} ({stats.elapsed_seconds:.1f}s)")
    if stats.conflicts:
        print(f"[tiingo] {stats.conflicts} conflicts recorded in batch params.")
    return 0 if stats.symbols_failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Tiingo corporate-actions ingest.")
    p.add_argument("--symbols", nargs="+", help="tickers (default: all seeded)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap to first N seeded securities (ignored with --symbols)")
    p.add_argument("--start", type=date.fromisoformat, default=None,
                   help="history start (YYYY-MM-DD); default 2016-01-01")
    p.add_argument("--incremental", action="store_true",
                   help="pull only a recent window per symbol (steady-state refresh)")
    p.add_argument("--dry-run", action="store_true", help="fetch + derive, no writes")
    args = p.parse_args(argv)

    config = TiingoConfig()
    client = TiingoClient(config)
    if args.incremental:
        start = date.today() - timedelta(days=INCREMENTAL_WINDOW_DAYS)
    else:
        start = args.start or HISTORY_START

    with psycopg.connect(settings.pg_conninfo()) as conn:
        if args.symbols:
            requested = [s.strip().upper() for s in args.symbols]
        else:
            requested = _all_seeded_symbols(conn, args.limit)
        return run(conn, config, client, requested, start, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())