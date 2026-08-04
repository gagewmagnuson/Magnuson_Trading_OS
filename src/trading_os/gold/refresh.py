"""
Gold refresh — materialize the wide Gold feature layer (DEC-028).

Repo path: src/trading_os/gold/refresh.py
Run:
  dry-run (plan only):  python -m trading_os.gold.refresh --dry-run
  refresh a range:      python -m trading_os.gold.refresh --start 2024-01-01 --end 2024-12-31
  full rebuild:         python -m trading_os.gold.refresh --all

Reads the canonical research read surface bars_eod_asof(adjustment=split) — Gold
contains NO adjustment logic — computes the Phase 1 features per security, and
writes wide monthly Parquet to lake/gold/. Consumers read Gold directly; the
Trading OS is a batch publisher (DEC-028 Decision 1).

Symbol is PIT-CORRECT: resolved from sec.security_identifier intervals by
session_date containment, NOT taken from the stored bar symbol — Gold stores what
was TRUE, so a reused ticker never mislabels an earlier company's rows.

knowledge_time propagates from each bar (DEC-028 Decision 3). Monthly partitions,
atomic write, idempotent (rewrite of a month replaces it) — mirrors the silver
incremental writer.
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import psycopg

from trading_os.config import settings
from trading_os.connectors.tiingo.config import TiingoConfig
from trading_os.engine.store import DuckDBStore
from . import RESEARCH_ADJUSTMENT, GOLD_COLUMNS, compute_gold_for_security


def _gold_dir(config: TiingoConfig) -> Path:
    return config.lake_root / "gold" / "features_eod"


def _month_file(gold_dir: Path, y: int, m: int) -> Path:
    return gold_dir / f"gold_features_{y:04d}_{m:02d}.parquet"


def _pit_symbols(conn: psycopg.Connection, security_ids: list[int]) -> dict[int, list[tuple]]:
    """For each security, its TICKER identifier intervals as
    (valid_from, valid_to_or_none, symbol), ordered. Used to assign each session
    the symbol valid on that date (PIT-correct, reuse-safe)."""
    rows = conn.execute(
        """
        select security_id, valid_from, valid_to, id_value
          from sec.security_identifier
         where id_type = 'TICKER' and security_id = any(%s)
         order by security_id, valid_from
        """,
        (security_ids,),
    ).fetchall()
    out: dict[int, list[tuple]] = {}
    for sid, vf, vt, sym in rows:
        out.setdefault(sid, []).append((vf, vt, sym))
    return out


def _symbol_for(intervals: list[tuple], d: date) -> str | None:
    """The TICKER valid on session_date d (valid_from <= d < valid_to)."""
    for vf, vt, sym in intervals:
        if vf <= d and (vt is None or d < vt):
            return sym
    return None


def run(start: date | None, end: date | None, do_all: bool, dry_run: bool,
        security_id: int | None = None) -> int:
    config = TiingoConfig()
    gold_dir = _gold_dir(config)
    as_of = date.today()   # reconstruct Gold as-of today's knowledge (best current view)

    store = DuckDBStore()
    store.connect(attach_postgres=True)

    # Pull adjusted bars over the requested range (or all history).
    lo = None if (do_all or security_id) else start
    hi = None if (do_all or security_id) else end
    sec_filter = [security_id] if security_id else None
    bars = store.bars_eod_asof(as_of=as_of, security_ids=sec_filter, start=lo, end=hi,
                               adjustment=RESEARCH_ADJUSTMENT)
    store.close()
    if not bars:
        print("[gold] no bars in range; nothing to do.")
        return 0

    # bars tuples: (security_id, symbol, session_date, o,h,l,c, volume,
    #               trade_count, vwap, knowledge_time, source)
    df = pl.DataFrame(
        {
            "security_id": [b[0] for b in bars],
            "session_date": [b[2] for b in bars],
            "close": [b[6] for b in bars],
            "volume": [b[7] for b in bars],
            "knowledge_time": [b[10] for b in bars],
        }
    )
    sec_ids = sorted(df["security_id"].unique().to_list())

    # PIT-correct symbols from identifier intervals (reuse-safe).
    with psycopg.connect(settings.pg_conninfo()) as conn:
        intervals = _pit_symbols(conn, sec_ids)

    print(f"[gold] === refresh plan ({'DRY-RUN' if dry_run else 'WRITE'}) ===")
    print(f"  adjustment (research surface): {RESEARCH_ADJUSTMENT}")
    print(f"  securities: {len(sec_ids)}   bars: {len(bars)}   "
          f"range: {df['session_date'].min()}..{df['session_date'].max()}")
    if dry_run:
        print("[gold] dry-run — no write.")
        return 0

    # Compute Gold per security, assign PIT symbol, collect.
    all_gold: list[pl.DataFrame] = []
    for sid in sec_ids:
        sec_bars = df.filter(pl.col("security_id") == sid).sort("session_date")
        syms = intervals.get(sid, [])
        sec_bars = sec_bars.with_columns(
            pl.col("session_date").map_elements(
                lambda d, s=syms: _symbol_for(s, d), return_dtype=pl.Utf8
            ).alias("symbol")
        )
        all_gold.append(compute_gold_for_security(sec_bars))

    gold = pl.concat(all_gold).select(GOLD_COLUMNS)

    # Write wide monthly partitions (group by year-month of session_date).
    gold_dir.mkdir(parents=True, exist_ok=True)
    gold = gold.with_columns([
        pl.col("session_date").dt.year().alias("_y"),
        pl.col("session_date").dt.month().alias("_m"),
    ])
    written = 0
    for (y, m), part in gold.group_by(["_y", "_m"]):
        part = part.drop(["_y", "_m"])
        out_file = _month_file(gold_dir, y, m)
        tmp = out_file.with_suffix(".parquet.tmp")
        # Full rewrite of the month (idempotent — Gold is derived, so a month's
        # features are fully recomputable; last write wins).
        part.sort(["security_id", "session_date"]).write_parquet(tmp)
        tmp.replace(out_file)
        written += part.height
        print(f"[gold] wrote {out_file.name}: {part.height} rows")

    print(f"\n[gold] done. {written} gold rows across "
          f"{gold.select(['_y','_m']).unique().height} month partitions.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trading_os.gold.refresh")
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--all", action="store_true", help="rebuild all history")
    ap.add_argument("--security-id", type=int, default=None,
                    help="restrict to one security_id (full history; for testing)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not args.all and not args.security_id and not (args.start and args.end):
        ap.error("provide --all, --security-id, or both --start and --end")
    return run(args.start, args.end, args.all, args.dry_run, args.security_id)


if __name__ == "__main__":
    raise SystemExit(main())