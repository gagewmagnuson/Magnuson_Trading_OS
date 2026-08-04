"""Tests for the Gold wide-row assembly (compute_gold_for_security)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from trading_os.gold import compute_gold_for_security, GOLD_COLUMNS


def _bars(closes, sid=1, sym="TEST"):
    d0 = date(2020, 1, 1)
    kt0 = datetime(2020, 1, 1, 21, 0, tzinfo=timezone.utc)
    n = len(closes)
    return pl.DataFrame({
        "security_id": [sid] * n,
        "symbol": [sym] * n,
        "session_date": [d0 + timedelta(days=i) for i in range(n)],
        "close": [float(c) for c in closes],
        "volume": [1000 + i for i in range(n)],
        "knowledge_time": [kt0 + timedelta(days=i) for i in range(n)],
    })


def test_output_has_exact_gold_columns_in_order():
    g = compute_gold_for_security(_bars([100.0, 101.0, 102.0]))
    assert g.columns == GOLD_COLUMNS


def test_observables_carried_through():
    g = compute_gold_for_security(_bars([100.0, 110.0]))
    assert g["adj_close"].to_list() == [100.0, 110.0]
    assert g["adj_volume"].to_list() == [1000, 1001]
    assert g["symbol"].to_list() == ["TEST", "TEST"]


def test_features_assembled_correctly():
    # 21 rising closes -> return_1d present from row 1, sma20 at row 19
    g = compute_gold_for_security(_bars([float(i) for i in range(1, 22)]))
    r1 = g["return_1d"].to_list()
    assert r1[0] is None and r1[1] == pytest.approx(2/1 - 1)
    sma20 = g["sma20"].to_list()
    assert all(v is None for v in sma20[:19])
    assert sma20[19] == pytest.approx(10.5)          # mean(1..20)


def test_knowledge_time_preserved_per_row():
    g = compute_gold_for_security(_bars([100.0, 101.0]))
    kts = g["knowledge_time"].to_list()
    assert kts[0] == datetime(2020, 1, 1, 21, 0, tzinfo=timezone.utc)
    assert kts[1] == datetime(2020, 1, 2, 21, 0, tzinfo=timezone.utc)


def test_empty_input_yields_empty_gold():
    empty = pl.DataFrame(schema={
        "security_id": pl.Int64, "symbol": pl.Utf8, "session_date": pl.Date,
        "close": pl.Float64, "volume": pl.Int64, "knowledge_time": pl.Datetime,
    })
    g = compute_gold_for_security(empty)
    assert g.is_empty()