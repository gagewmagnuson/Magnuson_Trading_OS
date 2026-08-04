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
from datetime import date, timedelta
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


def refresh_gold(start: date | None, end: date | None, do_all: bool,
                 security_id: int | None = None,
                 emit_months: set[tuple[int, int]] | None = None,
                 dry_run: bool = False) -> int:
    """Core Gold refresh: read adjusted bars over [start,end] (or all), compute
    features, write wide monthly partitions. The single source of truth for Gold
    computation — every CLI mode funnels here.

    emit_months: if given, only these (year, month) partitions are WRITTEN; rows
    outside them are still computed (they serve as rolling-feature lookback) but
    discarded from output. None = write every month present in the data.
    """
    config = TiingoConfig()
    gold_dir = _gold_dir(config)
    as_of = date.today()

    store = DuckDBStore()
    store.connect(attach_postgres=True)
    lo = None if (do_all or security_id) else start
    hi = None if (do_all or security_id) else end
    sec_filter = [security_id] if security_id else None
    bars = store.bars_eod_asof(as_of=as_of, security_ids=sec_filter, start=lo, end=hi,
                               adjustment=RESEARCH_ADJUSTMENT)
    store.close()
    if not bars:
        print("[gold] no bars in range; nothing to do.")
        return 0

    df = pl.DataFrame({
        "security_id": [b[0] for b in bars],
        "session_date": [b[2] for b in bars],
        "close": [b[6] for b in bars],
        "volume": [b[7] for b in bars],
        "knowledge_time": [b[10] for b in bars],
    })
    sec_ids = sorted(df["security_id"].unique().to_list())

    with psycopg.connect(settings.pg_conninfo()) as conn:
        intervals = _pit_symbols(conn, sec_ids)

    print(f"[gold] === refresh plan ({'DRY-RUN' if dry_run else 'WRITE'}) ===")
    print(f"  adjustment (research surface): {RESEARCH_ADJUSTMENT}")
    print(f"  securities: {len(sec_ids)}   bars: {len(bars)}   "
          f"range: {df['session_date'].min()}..{df['session_date'].max()}")
    if emit_months is not None:
        print(f"  emit months (only these written): {sorted(emit_months)}")
    if dry_run:
        print("[gold] dry-run — no write.")
        return 0

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

    gold = pl.concat(all_gold).select(GOLD_COLUMNS).with_columns([
        pl.col("session_date").dt.year().alias("_y"),
        pl.col("session_date").dt.month().alias("_m"),
    ])

    gold_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    months_written = 0
    for (y, m), part in gold.group_by(["_y", "_m"]):
        if emit_months is not None and (y, m) not in emit_months:
            continue   # computed as lookback, not emitted
        part = part.drop(["_y", "_m"])
        out_file = _month_file(gold_dir, y, m)
        tmp = out_file.with_suffix(".parquet.tmp")
        part.sort(["security_id", "session_date"]).write_parquet(tmp)
        tmp.replace(out_file)
        written += part.height
        months_written += 1
        print(f"[gold] wrote {out_file.name}: {part.height} rows")

    print(f"\n[gold] done. {written} rows across {months_written} month partitions.")
    return 0


def _nightly_plan(today: date) -> tuple[date, set[tuple[int, int]]]:
    """Compute (lookback_start, emit_months) for the nightly refresh: the current
    month always, plus the previous month during the first days of a new month
    (self-healing window for late corrections). Lookback = 252 sessions before the
    earliest emit month's start, so rolling features warm up correctly."""
    from trading_os.bars.knowledge_time import sessions_between
    cur = (today.year, today.month)
    emit = {cur}
    earliest_month_start = date(today.year, today.month, 1)
    if today.day <= 5:
        prev_start = (earliest_month_start - timedelta(days=1)).replace(day=1)
        emit.add((prev_start.year, prev_start.month))
        earliest_month_start = prev_start
    # 252 sessions before earliest_month_start: reach back ~400 calendar days,
    # take the last 252 sessions, use the earliest as lookback_start.
    candidates = sessions_between(earliest_month_start - timedelta(days=400),
                                  earliest_month_start)
    lookback_start = candidates[-252] if len(candidates) >= 252 else (
        candidates[0] if candidates else earliest_month_start)
    return lookback_start, emit


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trading_os.gold.refresh")
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--all", action="store_true", help="rebuild all history")
    ap.add_argument("--nightly", action="store_true",
                    help="refresh open partitions (current month, prev month early "
                         "in a new month) with 252-session lookback")
    ap.add_argument("--security-id", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.nightly:
        lookback_start, emit = _nightly_plan(date.today())
        return refresh_gold(start=lookback_start, end=date.today(), do_all=False,
                            emit_months=emit, dry_run=args.dry_run)
    if not args.all and not args.security_id and not (args.start and args.end):
        ap.error("provide --all, --nightly, --security-id, or both --start and --end")
    return refresh_gold(args.start, args.end, args.all, args.security_id,
                        emit_months=None, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())