"""
EOD bars point-in-time test (bitemporal correctness).

Self-contained: writes a tiny synthetic bars_eod Parquet into a temp lake and
reads it back through the generic engine reader (DuckDBStore.bars_eod_asof) with
NO Postgres attach and NO network. Proves the two invariants the whole system
exists to guarantee for price bars:

  1. No lookahead — a bar is invisible for any as_of < its knowledge_time.
  2. Latest-known-wins — when a bar is revised (same session, later
     knowledge_time), the version current as-of the query date is returned, and
     an earlier as_of still returns the original.

Mirrors the synthetic, self-contained style of the macro/security-master tests;
needs only DuckDB, no live data.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import pytest

from trading_os.engine.config import EngineConfig
from trading_os.engine.store import DuckDBStore

KT_ORIG = datetime(2020, 1, 2, 20, 0, tzinfo=timezone.utc)   # original 01-02 bar
KT_0103 = datetime(2020, 1, 3, 20, 0, tzinfo=timezone.utc)   # original 01-03 bar
KT_REV = datetime(2020, 1, 10, 20, 0, tzinfo=timezone.utc)   # revised 01-02 bar


def _write_synthetic_bars(silver_dir):
    silver_dir.mkdir(parents=True, exist_ok=True)
    out = silver_dir / "bars_eod_batch_test.parquet"
    rows = [
        # security_id, symbol, session_date, o, h, l, c, vol, n, vw, kt, batch, source
        (1, "TEST", date(2020, 1, 2), 100.0, 101.0, 99.0, 100.0, 1000, 10, 100.0, KT_ORIG, 1, "TEST"),
        (1, "TEST", date(2020, 1, 3), 100.0, 102.0, 99.5, 101.0, 1100, 11, 101.0, KT_0103, 1, "TEST"),
        # a revision of the 2020-01-02 close, known only from 2020-01-10:
        (1, "TEST", date(2020, 1, 2), 100.0, 101.0, 99.0, 105.0, 1000, 10, 100.0, KT_REV, 2, "TEST"),
    ]
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
        con.execute(f"copy bars to '{out.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


@pytest.fixture
def bars_store(tmp_path):
    lake = tmp_path / "lake"
    _write_synthetic_bars(lake / "silver" / "bars_eod")
    st = DuckDBStore(EngineConfig(lake_root=lake))
    st.connect(attach_postgres=False)  # pure Parquet: no Postgres needed
    yield st
    st.close()


def _close_by_date(rows):
    # rows: (security_id, symbol, session_date, o, h, l, close, vol, n, vw, kt, source)
    return {r[2]: r[6] for r in rows}


def test_no_lookahead(bars_store):
    # As of 2020-01-02: only the original 01-02 bar is known. The 01-03 bar
    # (knowledge_time 01-03) and the revision (01-10) must be invisible.
    by_date = _close_by_date(bars_store.bars_eod_asof(date(2020, 1, 2), security_ids=[1]))
    assert by_date.get(date(2020, 1, 2)) == 100.0, "original 01-02 close should be visible"
    assert date(2020, 1, 3) not in by_date, "a bar must be invisible before its knowledge_time"


def test_revision_not_visible_early(bars_store):
    # As of 2020-01-05: 01-02 still shows the ORIGINAL close (revision known only
    # from 01-10); 01-03 is now visible.
    by_date = _close_by_date(bars_store.bars_eod_asof(date(2020, 1, 5), security_ids=[1]))
    assert by_date[date(2020, 1, 2)] == 100.0, "revision must not be visible before its knowledge_time"
    assert by_date[date(2020, 1, 3)] == 101.0


def test_latest_known_wins(bars_store):
    # As of 2020-01-15: the revised 01-02 close (knowledge_time 01-10) wins.
    by_date = _close_by_date(bars_store.bars_eod_asof(date(2020, 1, 15), security_ids=[1]))
    assert by_date[date(2020, 1, 2)] == 105.0, "latest knowledge_time <= as_of must win"
    assert by_date[date(2020, 1, 3)] == 101.0


def test_empty_before_any_knowledge(bars_store):
    # As of 2019-12-31: nothing is known yet.
    assert bars_store.bars_eod_asof(date(2019, 12, 31), security_ids=[1]) == []