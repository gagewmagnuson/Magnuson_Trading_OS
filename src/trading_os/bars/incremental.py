"""
Forward-capture writer: append a session's bars into a monthly Parquet partition.

Repo path: src/trading_os/bars/incremental.py

Distinct from bars/writer.py (immutable, append-only batch files for the historical
reconstruction). This is the LIVING dataset — daily capture, one session at a time,
merged into a per-month file via read-modify-write. Keeping them separate keeps
each writer single-purpose (the reconstruction writer is never rewritten; this one
always is).

Guarantees:
  - one file per calendar month: bars_eod_YYYY_MM.parquet
  - idempotent: re-writing a session already present replaces those rows (dedup on
    (security_id, session_date), latest knowledge_time wins) — no duplicates
  - atomic: builds a tmp file and renames over the month file, so the lake glob
    never sees a partial file
  - same knowledge_time (DEC-024, session-close) and schema as the batch writer,
    so reads and DQ persistence work unchanged

Caller contract: `bars` belong to a SINGLE calendar month (the incremental command
processes one session at a time, so this always holds). A month mismatch raises.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb

from .knowledge_time import market_close_knowledge_time, NonSessionDateError
from .models import Bar
from .writer import SkippedBar, WriteResult, _knowledge_time_for


def _month_file(silver_dir: Path, y: int, m: int) -> Path:
    return silver_dir / f"bars_eod_{y:04d}_{m:02d}.parquet"


# The canonical bars schema (must match write_bars_parquet's column order).
_COLS = [
    "security_id", "symbol", "session_date", "open", "high", "low", "close",
    "volume", "trade_count", "vwap", "knowledge_time", "batch_id", "source",
]


def update_monthly_partition(
    bars: list[Bar],
    silver_dir: Path,
    source: str,
    batch_id: int,
) -> WriteResult:
    """Merge one session's `bars` (single calendar month) into that month's file.

    Reads the existing month file (if any), appends the new rows, dedups on
    (security_id, session_date) keeping the latest knowledge_time, writes a tmp
    file and atomically renames. Returns rows written (net new/updated) + skipped.
    """
    if not bars:
        return WriteResult(rows_written=0, skipped=[])

    # Enforce the single-month contract.
    months = {(b.session_date.year, b.session_date.month) for b in bars}
    if len(months) != 1:
        raise ValueError(f"update_monthly_partition expects one calendar month, got {sorted(months)}")
    (year, month), = months

    silver_dir.mkdir(parents=True, exist_ok=True)
    out_file = _month_file(silver_dir, year, month)
    tmp_file = out_file.with_suffix(".parquet.tmp")

    # Derive kt per session; collect new rows and skips (mirror the batch writer).
    kt_cache: dict[date, datetime | None] = {}
    new_rows: list[tuple] = []
    skipped: list[SkippedBar] = []
    for b in bars:
        kt = _knowledge_time_for(b.session_date, kt_cache)
        if kt is None:
            skipped.append(SkippedBar(
                security_id=b.security_id, symbol=b.symbol,
                session_date=b.session_date, reason="non_session_date",
            ))
            continue
        new_rows.append((
            b.security_id, b.symbol, b.session_date.isoformat(),
            b.open, b.high, b.low, b.close, b.volume,
            None if b.trade_count is None else b.trade_count,
            None if b.vwap is None else b.vwap,
            kt.isoformat(), batch_id, source,
        ))

    if not new_rows:
        return WriteResult(rows_written=0, skipped=skipped)

    con = duckdb.connect()
    try:
        con.execute("set timezone='UTC'")
        # Staging table with the exact schema.
        con.execute(f"""
            create table staged (
                security_id bigint, symbol varchar, session_date date,
                open double, high double, low double, close double,
                volume bigint, trade_count bigint, vwap double,
                knowledge_time timestamptz, batch_id bigint, source varchar
            )
        """)
        con.executemany(
            "insert into staged values (?,?,?,?,?,?,?,?,?,?,?,?,?)", new_rows
        )
        # If the month file exists, union it in; else just the staged rows.
        if out_file.exists():
            con.execute(f"""
                create table merged as
                select * from read_parquet('{out_file.as_posix()}')
                union all by name
                select * from staged
            """)
        else:
            con.execute("create table merged as select * from staged")

        # Dedup: one row per (security_id, session_date), latest knowledge_time wins.
        con.execute(f"""
            copy (
                select {', '.join(_COLS)} from (
                    select *, row_number() over (
                        partition by security_id, session_date
                        order by knowledge_time desc, batch_id desc
                    ) as rn
                    from merged
                ) where rn = 1
                order by security_id, session_date
            ) to '{tmp_file.as_posix()}' (format parquet)
        """)
        rows_written = con.execute(
            f"select count(*) from read_parquet('{tmp_file.as_posix()}')"
        ).fetchone()[0]
    finally:
        con.close()

    tmp_file.replace(out_file)   # atomic
    return WriteResult(rows_written=rows_written, skipped=skipped)