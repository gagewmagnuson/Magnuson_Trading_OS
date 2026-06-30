"""
Bars DQ tests. Two halves:

  * Pure gap logic (compute_gaps) — no DB, no lake. Proves the per-security
    window invariant: a late-listed security is NEVER accused of missing
    pre-listing sessions (listing_era_leak == 0), while a true interior gap is
    caught.
  * Pure-Parquet checks (sanity, duplicates, zero_volume) — synthetic lake in a
    temp dir, engine connected WITHOUT Postgres. Proves each defect lands in the
    right tier. (coverage/gaps/freshness are cross-store and exercised against
    the real lake by the CLI.)
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import pytest

from trading_os.dq import bars as dq
from trading_os.engine.config import EngineConfig
from trading_os.engine.store import DuckDBStore

KT = datetime(2020, 1, 10, 20, 0, tzinfo=timezone.utc)


# ----------------------- pure gap logic -----------------------
def test_compute_gaps_catches_interior_gap():
    cal = [date(2020, 1, d) for d in (2, 3, 6, 7, 8)]  # 5 sessions
    # security observed all but 2020-01-07 (an interior gap)
    observed = {1: ("AAA", {date(2020, 1, 2), date(2020, 1, 3),
                            date(2020, 1, 6), date(2020, 1, 8)})}
    r = dq.compute_gaps(observed, cal)
    assert r.interior_total == 1
    assert r.interior_by_security == [(1, "AAA", 1)]
    assert r.listing_era_leak == 0


def test_compute_gaps_ignores_pre_listing_sessions():
    # Calendar starts 2020-01-02, but this security's first bar is 2020-01-07.
    # The four earlier sessions must NOT be counted as gaps (listing-era).
    cal = [date(2020, 1, d) for d in (2, 3, 6, 7, 8)]
    observed = {9: ("LATE", {date(2020, 1, 7), date(2020, 1, 8)})}
    r = dq.compute_gaps(observed, cal)
    assert r.interior_total == 0, "pre-listing sessions must not be flagged"
    assert r.listing_era_leak == 0
    assert r.interior_by_security == []


# ----------------------- synthetic lake for pure-Parquet checks -----------------------
def _write_bars(silver_dir, rows):
    silver_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            """
            create table bars (
                security_id BIGINT, symbol VARCHAR, session_date DATE,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                volume BIGINT, trade_count BIGINT, vwap DOUBLE,
                knowledge_time TIMESTAMPTZ, ingest_batch_id BIGINT, source VARCHAR)
            """
        )
        con.executemany("insert into bars values (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.execute(
            f"copy bars to '{(silver_dir / 'b.parquet').as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()


def _row(sid, sym, d, o, h, l, c, v, kt=KT, batch=1):
    return (sid, sym, d, o, h, l, c, v, 10, o, kt, batch, "TEST")


@pytest.fixture
def store(tmp_path):
    lake = tmp_path / "lake"
    rows = [
        _row(1, "AAA", date(2020, 1, 2), 10, 11, 9, 10, 1000),   # clean
        _row(2, "BAD", date(2020, 1, 2), 10, 8, 9, 10, 1000),    # high<low
        _row(3, "ZER", date(2020, 1, 2), 10, 11, 9, 10, 0),      # zero volume
        # a true duplicate: same (security_id, session_date, knowledge_time) twice
        _row(4, "DUP", date(2020, 1, 2), 10, 11, 9, 10, 1000),
        _row(4, "DUP", date(2020, 1, 2), 10, 11, 9, 10, 1000),
    ]
    _write_bars(lake / "silver" / "bars_eod", rows)
    st = DuckDBStore(EngineConfig(lake_root=lake))
    st.connect(attach_postgres=False)
    yield st
    st.close()


def test_sanity_catches_high_lt_low(store):
    s = dq.sanity(store)
    assert s.high_lt_low == 1
    assert s.violations >= 1


def test_duplicates_catches_repeated_pit_key(store):
    d = dq.duplicates(store)
    assert d.count == 1  # one (security_id, session_date, knowledge_time) group


def test_zero_volume_flagged(store):
    z = dq.zero_volume(store)
    assert z.count == 1
    assert ("ZER", date(2020, 1, 2)) in z.examples