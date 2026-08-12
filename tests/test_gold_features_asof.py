"""Tests for DuckDBStore.gold_features_asof — the PIT read over the gold lake.

Mirrors the bars_eod_asof knowledge_time discipline (DEC-004 semantics) applied
to derived gold features: for each (security_id, session_date), the row with the
latest knowledge_time <= as_of wins; nothing knowable only after as_of leaks in.

Hermetic: builds a tiny synthetic gold lake in tmp_path, so it never touches the
real lake and runs without network or the full dataset.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from pathlib import Path
import duckdb
import pytest
from trading_os.engine.config import EngineConfig
from trading_os.engine.store import DuckDBStore

# The gold schema (confirmed against lake/gold/features_eod):
#   security_id, symbol, session_date, knowledge_time, adj_close, adj_volume,
#   return_1d, log_return_1d, sma20, sma50, ema20, realized_vol20, roc20, momentum_12_1
_GOLD_COLS = (
    "security_id BIGINT, symbol VARCHAR, session_date DATE, "
    "knowledge_time TIMESTAMPTZ, adj_close DOUBLE, adj_volume BIGINT, "
    "return_1d DOUBLE, log_return_1d DOUBLE, sma20 DOUBLE, sma50 DOUBLE, "
    "ema20 DOUBLE, realized_vol20 DOUBLE, roc20 DOUBLE, momentum_12_1 DOUBLE"
)


def _kt(y, m, d):
    return datetime(y, m, d, 21, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def gold_lake(tmp_path):
    """Build a synthetic gold lake under tmp_path and return an EngineConfig
    pointed at it. Data is designed to exercise the knowledge_time dedup:

      security 1, session 2020-01-06 has TWO knowledge_time versions:
        - momentum_12_1 = 0.10, knowledge_time 2020-01-06 (original)
        - momentum_12_1 = 0.99, knowledge_time 2020-02-15 (a later recompute)
      A read as_of 2020-01-31 must see 0.10; as_of 2020-03-01 must see 0.99.
    """
    gold_dir = tmp_path / "gold" / "features_eod"
    gold_dir.mkdir(parents=True)
    con = duckdb.connect()
    con.execute(f"CREATE TABLE g ({_GOLD_COLS})")

    def row(sid, sym, d, kt, mom):
        con.execute(
            "INSERT INTO g VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [sid, sym, d, kt, 100.0, 1000, 0.01, 0.01, 99.0, 98.0, 99.5, 0.2, 0.05, mom],
        )

    # security 1: two sessions; the second has two knowledge_time versions.
    row(1, "AAA", date(2020, 1, 3), _kt(2020, 1, 3), 0.05)
    row(1, "AAA", date(2020, 1, 6), _kt(2020, 1, 6), 0.10)   # original
    row(1, "AAA", date(2020, 1, 6), _kt(2020, 2, 15), 0.99)  # later recompute
    # security 2: one clean session.
    row(2, "BBB", date(2020, 1, 6), _kt(2020, 1, 6), 0.20)

    con.execute(
        f"COPY g TO '{(gold_dir / 'gold_features_2020_01.parquet').as_posix()}' (FORMAT PARQUET)"
    )
    con.close()
    return EngineConfig(lake_root=tmp_path)


def _store(config):
    s = DuckDBStore(config)
    s.connect(attach_postgres=False)  # gold is pure Parquet, no Postgres needed
    return s


def test_returns_latest_version_known_by_as_of(gold_lake):
    """As of 2020-01-31, the later (2020-02-15) recompute is NOT yet knowable,
    so security 1 / 2020-01-06 must show the original momentum 0.10."""
    s = _store(gold_lake)
    rows = s.gold_features_asof(date(2020, 1, 31), security_ids=[1],
                                start=date(2020, 1, 6), end=date(2020, 1, 6))
    s.close()
    assert len(rows) == 1
    # momentum_12_1 is the last column (index 13).
    assert rows[0][13] == pytest.approx(0.10)


def test_later_recompute_appears_after_its_knowledge_time(gold_lake):
    """As of 2020-03-01, the 2020-02-15 recompute IS knowable, so the same
    session now shows the revised momentum 0.99 — and only ONE row (dedup)."""
    s = _store(gold_lake)
    rows = s.gold_features_asof(date(2020, 3, 1), security_ids=[1],
                                start=date(2020, 1, 6), end=date(2020, 1, 6))
    s.close()
    assert len(rows) == 1                       # dedup: not two rows
    assert rows[0][13] == pytest.approx(0.99)


def test_no_lookahead_before_any_knowledge(gold_lake):
    """As of 2020-01-05, session 2020-01-06 is not yet knowable at all -> empty."""
    s = _store(gold_lake)
    rows = s.gold_features_asof(date(2020, 1, 5), security_ids=[1])
    s.close()
    # Only the 2020-01-03 session is knowable by 2020-01-05.
    sessions = {r[2] for r in rows}
    assert date(2020, 1, 6) not in sessions
    assert date(2020, 1, 3) in sessions


def test_security_filter_and_ordering(gold_lake):
    """Multiple securities return ordered by (security_id, session_date)."""
    s = _store(gold_lake)
    rows = s.gold_features_asof(date(2020, 3, 1))  # all securities
    s.close()
    ids_dates = [(r[0], r[2]) for r in rows]
    assert ids_dates == sorted(ids_dates)
    assert (2, date(2020, 1, 6)) in ids_dates


def test_returns_full_feature_row(gold_lake):
    """The reader returns the full gold row (14 columns), not a single feature."""
    s = _store(gold_lake)
    rows = s.gold_features_asof(date(2020, 3, 1), security_ids=[2])
    s.close()
    assert len(rows[0]) == 14   # full row: identity + kt + adj_* + 8 features