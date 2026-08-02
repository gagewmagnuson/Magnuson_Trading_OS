"""Tests for the monthly-partition forward-capture writer (read-modify-write, dedup)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from trading_os.bars.incremental import update_monthly_partition, _month_file
from trading_os.bars.models import Bar


def _bar(sid, sym, d, close=100.0):
    return Bar(security_id=sid, symbol=sym, session_date=d,
               open=close, high=close, low=close, close=close,
               volume=1000, trade_count=None, vwap=None)


def _read(path):
    con = duckdb.connect()
    rows = con.execute(
        f"select security_id, session_date, close, batch_id "
        f"from read_parquet('{path.as_posix()}') order by security_id, session_date"
    ).fetchall()
    con.close()
    return rows


def test_creates_month_file_when_absent(tmp_path):
    # 2026-07-31 is a Friday (session)
    bars = [_bar(1, "AAPL", date(2026, 7, 31)), _bar(2, "MSFT", date(2026, 7, 31))]
    res = update_monthly_partition(bars, tmp_path, "TIINGO", batch_id=10)
    f = _month_file(tmp_path, 2026, 7)
    assert f.exists()
    assert res.rows_written == 2
    assert res.skipped == []


def test_appends_second_session_into_same_month(tmp_path):
    # 2026-07-30 Thursday, 2026-07-31 Friday — both sessions, same month
    update_monthly_partition([_bar(1, "AAPL", date(2026, 7, 30))], tmp_path, "TIINGO", 10)
    update_monthly_partition([_bar(1, "AAPL", date(2026, 7, 31))], tmp_path, "TIINGO", 11)
    rows = _read(_month_file(tmp_path, 2026, 7))
    assert len(rows) == 2   # both sessions present in the one month file
    assert {r[1] for r in rows} == {date(2026, 7, 30), date(2026, 7, 31)}


def test_reingest_same_session_dedups_latest_wins(tmp_path):
    # first write close=100 (batch 10), re-write same session close=200 (batch 11)
    update_monthly_partition([_bar(1, "AAPL", date(2026, 7, 31), close=100.0)], tmp_path, "TIINGO", 10)
    update_monthly_partition([_bar(1, "AAPL", date(2026, 7, 31), close=200.0)], tmp_path, "TIINGO", 11)
    rows = _read(_month_file(tmp_path, 2026, 7))
    assert len(rows) == 1           # no duplicate
    assert rows[0][2] == 200.0      # latest knowledge_time (batch 11) won
    assert rows[0][3] == 11         # and carries batch 11


def test_non_session_date_is_skipped(tmp_path):
    # 2026-08-02 is a Sunday -> non-session -> skipped, not written
    res = update_monthly_partition([_bar(1, "AAPL", date(2026, 8, 2))], tmp_path, "TIINGO", 10)
    assert res.rows_written == 0
    assert len(res.skipped) == 1 and res.skipped[0].reason == "non_session_date"
    assert not _month_file(tmp_path, 2026, 8).exists()


def test_rejects_multi_month_input(tmp_path):
    # dates in two different months -> the single-month guard raises before any write
    bars = [_bar(1, "AAPL", date(2026, 7, 31)), _bar(1, "AAPL", date(2026, 8, 31))]
    with pytest.raises(ValueError):
        update_monthly_partition(bars, tmp_path, "TIINGO", 10)