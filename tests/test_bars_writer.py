"""
Tests for the shared bars silver writer (DEC-024).

Hermetic: writes to a tmp_path, reads the Parquet back with DuckDB. Asserts
per-session knowledge_time, non-session skip behavior, and the structured anomaly
list — the DEC-024 strict-core/graceful-boundary contract.
"""
from __future__ import annotations

from datetime import date, timezone

import duckdb

from trading_os.bars.models import Bar
from trading_os.bars.writer import write_bars_parquet
from trading_os.bars.knowledge_time import market_close_knowledge_time


def _bar(session_date, sec_id=1, symbol="AAPL"):
    return Bar(
        security_id=sec_id, symbol=symbol, session_date=session_date,
        open=1.0, high=2.0, low=0.5, close=1.5, volume=1000,
        trade_count=10, vwap=1.4,
    )


def test_writes_valid_bars_with_derived_knowledge_time(tmp_path):
    bars = [_bar(date(2020, 1, 3)), _bar(date(2020, 7, 1))]
    res = write_bars_parquet(bars, tmp_path, "TESTSRC", batch_id=42)

    assert res.rows_written == 2
    assert res.skipped == []

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    rows = con.execute(
        f"SELECT session_date, knowledge_time, source "
        f"FROM '{(tmp_path / 'bars_eod_batch_42.parquet').as_posix()}' "
        f"ORDER BY session_date"
    ).fetchall()
    con.close()

    # knowledge_time is the derived session close, not an ingest wall clock.
    assert rows[0][1] == market_close_knowledge_time(date(2020, 1, 3))
    assert rows[1][1] == market_close_knowledge_time(date(2020, 7, 1))
    assert rows[0][2] == "TESTSRC"


def test_non_session_bar_is_skipped_not_written(tmp_path):
    bars = [
        _bar(date(2020, 1, 3)),                 # valid session
        _bar(date(2020, 1, 4), symbol="BAD"),   # Saturday -> skip
        _bar(date(2020, 12, 25), symbol="XMAS"),  # holiday -> skip
    ]
    res = write_bars_parquet(bars, tmp_path, "TESTSRC", batch_id=7)

    assert res.rows_written == 1
    reasons = {(s.symbol, s.reason) for s in res.skipped}
    assert reasons == {("BAD", "non_session_date"), ("XMAS", "non_session_date")}


def test_all_skipped_writes_no_file(tmp_path):
    bars = [_bar(date(2020, 1, 4))]  # Saturday only
    res = write_bars_parquet(bars, tmp_path, "TESTSRC", batch_id=99)
    assert res.rows_written == 0
    assert len(res.skipped) == 1
    assert not (tmp_path / "bars_eod_batch_99.parquet").exists()


def test_empty_input_writes_no_file(tmp_path):
    res = write_bars_parquet([], tmp_path, "TESTSRC", batch_id=1)
    assert res.rows_written == 0
    assert res.skipped == []
    assert not (tmp_path / "bars_eod_batch_1.parquet").exists()