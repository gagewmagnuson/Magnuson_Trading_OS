"""
Alpaca EOD bars CLI. Fetches unadjusted daily bars for seeded securities and
writes them to the silver Parquet lake with a bitemporal knowledge_time.

Targets come from the security master (resolve-and-skip, DEC-017). By default it
fetches every seeded security; --symbols or --limit narrow it (use --symbols for
the validation cohort). --start overrides the 2016-01-01 backfill floor.

Usage (from repo root, venv active, Alpaca keys in .env):
    python -m trading_os.connectors.alpaca.cli --symbols AAPL,MSFT,GOOGL,AMZN,JPM
    python -m trading_os.connectors.alpaca.cli --limit 20
    python -m trading_os.connectors.alpaca.cli                 # full seeded universe
    python -m trading_os.connectors.alpaca.cli --symbols AAPL --dry-run
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

import psycopg

from trading_os.config import settings

from trading_os.bars.writer import write_bars_parquet

from .client import AlpacaClient
from .config import AlpacaConfig
from .parser import parse_bars
from .writer import BarsWriter


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


def run(conn, config: AlpacaConfig, client: AlpacaClient,
        requested: list[str], start: date, dry_run: bool) -> int:
    writer = BarsWriter(conn, config)
    sec_map = writer.resolve_security_ids(requested)
    skipped_syms = [s for s in requested if s not in sec_map]
    print(f"[alpaca] {len(requested)} symbols requested, {len(sec_map)} resolved, "
          f"{len(skipped_syms)} skipped"
          + (f" e.g. {', '.join(skipped_syms[:8])}" if skipped_syms else ""))
    if not sec_map:
        print("[alpaca] nothing to fetch (no requested symbol is in the master).")
        return 0

    ref, raw = client.fetch_daily_bars(list(sec_map.keys()), start)
    bars = parse_bars(raw, sec_map)
    raw_count = sum(len(v) for v in raw.values())
    print(f"[alpaca] fetched {raw_count} raw bars from {start} -> {len(bars)} parsed; "
          f"bronze={ref.path}")

    if dry_run:
        print("[alpaca] dry-run: no Parquet or batch written.")
        return 0

    # Batch knowledge_time is the INGEST timestamp — lineage for "when the system
    # fetched" (meta.ingest_batch). It is distinct from each bar's per-session
    # knowledge_time, which the shared writer derives (DEC-024 Rule 1).
    ingest_kt = datetime.now(timezone.utc)
    source_id = writer.ensure_source()
    batch_id = writer.open_batch(
        source_id, ingest_kt,
        {"symbols": len(sec_map), "start": start.isoformat(),
         "feed": config.feed, "adjustment": config.adjustment, "bronze": ref.path},
    )
    try:
        result = write_bars_parquet(bars, config.silver_dir, "ALPACA", batch_id)
        writer.close_batch(batch_id, "succeeded",
                           rows_in=len(bars), rows_out=result.rows_written)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        try:
            writer.close_batch(batch_id, "failed", rows_in=len(bars), rows_out=0,
                               error=str(e))
            conn.commit()
        except Exception:
            conn.rollback()
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1

    print(f"[alpaca] wrote {result.rows_written} bars -> "
          f"{config.silver_dir}/bars_eod_batch_{batch_id}.parquet (batch {batch_id})")
    if result.skipped:
        print(f"[alpaca] DQ: skipped {len(result.skipped)} bar(s) on non-session "
              f"dates (not written):")
        for s in result.skipped[:10]:
            print(f"          {s.symbol} {s.session_date.isoformat()} — {s.reason}")
        if len(result.skipped) > 10:
            print(f"          ... and {len(result.skipped) - 10} more")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Alpaca EOD bars ingest -> silver Parquet.")
    p.add_argument("--symbols", help="comma-separated tickers (default: all seeded securities)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap to the first N seeded securities (ignored if --symbols given)")
    p.add_argument("--start", type=date.fromisoformat, default=None,
                   help="override backfill start (YYYY-MM-DD); default 2016-01-01")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch + parse, write nothing")
    args = p.parse_args(argv)

    config = AlpacaConfig()
    client = AlpacaClient(config)
    start = args.start or config.start

    with psycopg.connect(settings.pg_conninfo()) as conn:
        if args.symbols:
            requested = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        else:
            requested = _all_seeded_symbols(conn, args.limit)
        return run(conn, config, client, requested, start, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())