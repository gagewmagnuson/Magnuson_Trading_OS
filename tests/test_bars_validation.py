"""
Tests for the shared bars silver validator (DEC-024 rebuild).

Hermetic: builds small old/new silver Parquet sets in tmp_path via the shared
writer, then asserts the five-stage report catches the failure modes.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from trading_os.bars.models import Bar
from trading_os.bars.writer import write_bars_parquet
from trading_os.bars.validation import validate_rebuild


def _bar(sd, sec=1, close=1.5, volume=1000):
    return Bar(security_id=sec, symbol="AAPL", session_date=sd,
               open=1.0, high=2.0, low=0.5, close=close, volume=volume,
               trade_count=10, vwap=1.4)


def _write(tmp_path, name, bars, batch):
    d = tmp_path / name
    write_bars_parquet(bars, d, "TESTSRC", batch_id=batch)
    return f"{d.as_posix()}/*.parquet"


SESSIONS = [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6)]


def test_clean_rebuild_passes(tmp_path):
    old = _write(tmp_path, "old", [_bar(s) for s in SESSIONS], 1)
    new = _write(tmp_path, "new", [_bar(s) for s in SESSIONS], 2)
    rep = validate_rebuild(old, new, sample_size=100)
    assert rep.passed
    assert rep.old_rows == rep.new_rows == 3
    assert rep.duplicate_pairs == 0
    assert rep.coverage_missing == 0
    assert rep.knowledge_time_violations == 0
    assert rep.value_diffs == []


def test_lost_coverage_fails(tmp_path):
    old = _write(tmp_path, "old", [_bar(s) for s in SESSIONS], 1)
    new = _write(tmp_path, "new", [_bar(s) for s in SESSIONS[:2]], 2)  # dropped one
    rep = validate_rebuild(old, new, sample_size=100)
    assert not rep.passed
    assert rep.coverage_missing == 1
    assert rep.coverage_missing_samples[0] == (1, date(2020, 1, 6))


def test_duplicate_pair_fails(tmp_path):
    old = _write(tmp_path, "old", [_bar(s) for s in SESSIONS], 1)
    # new has two files both containing 2020-01-02 -> a duplicate pair in the glob
    d = tmp_path / "new"
    write_bars_parquet([_bar(s) for s in SESSIONS], d, "TESTSRC", batch_id=2)
    write_bars_parquet([_bar(date(2020, 1, 2))], d, "TESTSRC", batch_id=3)
    new = f"{d.as_posix()}/*.parquet"
    rep = validate_rebuild(old, new, sample_size=100)
    assert not rep.passed
    assert rep.duplicate_pairs == 1
    assert rep.duplicate_samples[0] == (1, date(2020, 1, 2))


def test_value_diff_surfaced_but_not_auto_fail(tmp_path):
    old = _write(tmp_path, "old", [_bar(s, close=1.5) for s in SESSIONS], 1)
    new = _write(tmp_path, "new", [_bar(s, close=(9.9 if s == SESSIONS[0] else 1.5))
                                    for s in SESSIONS], 2)
    rep = validate_rebuild(old, new, sample_size=100)
    assert rep.passed  # value diffs alone do not fail
    assert any(d.column == "close" and d.session_date == SESSIONS[0]
               for d in rep.value_diffs)