"""
Shared silver writer for the EOD bars dataset.

Repo path: src/trading_os/bars/writer.py

The source-independent mechanism that writes canonical Bars into the silver
Parquet lake. Every bars producer — the Alpaca connector, the Tiingo connector,
and the silver rebuild — calls this ONE function, so knowledge_time derivation,
session validation, and skipped-bar handling are identical across all sources
(DEC-024: bars semantics are dataset-scoped, not source-scoped). This mirrors the
shared corp/action_write primitive (DEC-019) one dataset over.

knowledge_time (DEC-024 Rule 1) is derived PER SESSION DATE via
bars.knowledge_time.market_close_knowledge_time — the exchange-close moment the
bar was objectively knowable — NOT the ingest wall clock. Derivations are cached
by session_date: a 35-year backfill of millions of bars spans only ~9,000
distinct trading days, so this is ~9,000 calendar lookups, not one per bar.

Strict core, graceful boundary: the knowledge_time primitive RAISES on a
non-session date (a vendor-data defect). This writer CATCHES that, records the
bar as a structured SkippedBar, skips it, and continues — so one bad bar never
fails a multi-million-row backfill ("missing beats wrong"). Skipped bars are
RETURNED (not just logged) so callers can report them and, later, persist them to
meta.dq_result without an API change.

The write itself preserves the proven path from the original Alpaca writer:
stream rows to a transient CSV (C-backed, flat memory), bulk-COPY into a typed
DuckDB table, COPY out to Parquet, atomic tmp-rename so a partial file never
appears in the lake glob.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb

from trading_os.bars.knowledge_time import (
    NonSessionDateError,
    market_close_knowledge_time,
)
from trading_os.bars.models import Bar

_BARS_DDL = """
create table bars (
    security_id     BIGINT,
    symbol          VARCHAR,
    session_date    DATE,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    close           DOUBLE,
    volume          BIGINT,
    trade_count     BIGINT,
    vwap            DOUBLE,
    knowledge_time  TIMESTAMPTZ,
    ingest_batch_id BIGINT,
    source          VARCHAR
)
"""


@dataclass(frozen=True)
class SkippedBar:
    """A bar excluded from the silver write, with the reason (DQ anomaly).

    Returned by write_bars_parquet so callers can report and (later) persist to
    meta.dq_result. Structured, not a log line, so it survives into tests and
    audits unchanged.
    """
    security_id: int
    symbol: str
    session_date: date
    reason: str


@dataclass(frozen=True)
class WriteResult:
    rows_written: int
    skipped: list[SkippedBar]


def _knowledge_time_for(
    session_date: date, cache: dict[date, datetime | None]
) -> datetime | None:
    """Cached per-session knowledge_time. None marks a known non-session date so
    repeated bad bars on the same date don't re-raise through the primitive."""
    if session_date not in cache:
        try:
            cache[session_date] = market_close_knowledge_time(session_date)
        except NonSessionDateError:
            cache[session_date] = None
    return cache[session_date]


def write_bars_parquet(
    bars: list[Bar],
    silver_dir: Path,
    source: str,
    batch_id: int,
) -> WriteResult:
    """Write canonical bars to one append-only Parquet file for this batch.

    Derives knowledge_time per session (DEC-024 Rule 1); skips bars on non-session
    dates as structured anomalies. Returns rows written + skipped list. Writes to
    a temp name and atomically renames, so a partial file never appears in the
    lake glob.
    """
    silver_dir.mkdir(parents=True, exist_ok=True)
    out_file = silver_dir / f"bars_eod_batch_{batch_id}.parquet"
    tmp_file = out_file.with_suffix(".parquet.tmp")
    tmp_csv = out_file.with_suffix(".csv.tmp")

    kt_cache: dict[date, datetime | None] = {}
    skipped: list[SkippedBar] = []
    rows_written = 0

    try:
        with tmp_csv.open("w", newline="") as f:
            w = csv.writer(f)
            for b in bars:
                kt = _knowledge_time_for(b.session_date, kt_cache)
                if kt is None:
                    skipped.append(
                        SkippedBar(
                            security_id=b.security_id,
                            symbol=b.symbol,
                            session_date=b.session_date,
                            reason="non_session_date",
                        )
                    )
                    continue
                w.writerow([
                    b.security_id, b.symbol, b.session_date.isoformat(),
                    b.open, b.high, b.low, b.close, b.volume,
                    "" if b.trade_count is None else b.trade_count,
                    "" if b.vwap is None else b.vwap,
                    kt.isoformat(), batch_id, source,
                ])
                rows_written += 1

        if rows_written == 0:
            # Nothing valid to write — no Parquet file is produced.
            return WriteResult(rows_written=0, skipped=skipped)

        con = duckdb.connect()
        try:
            con.execute("SET TimeZone='UTC'")
            con.execute(_BARS_DDL)
            con.execute(
                f"COPY bars FROM '{tmp_csv.as_posix()}' (FORMAT CSV, HEADER false)"
            )
            con.execute(f"COPY bars TO '{tmp_file.as_posix()}' (FORMAT PARQUET)")
        finally:
            con.close()
    finally:
        tmp_csv.unlink(missing_ok=True)

    tmp_file.replace(out_file)
    return WriteResult(rows_written=rows_written, skipped=skipped)