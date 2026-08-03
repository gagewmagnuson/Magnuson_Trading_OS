"""Tests for Phase 1 Gold features: known-value correctness + determinism + null warm-up."""
from __future__ import annotations

from datetime import date, timedelta

import math
import polars as pl
import pytest

from trading_os.features import (
    simple_return, log_return, sma, ema, realized_vol, roc, momentum_12_1,
)


def _frame(closes: list[float], sid: int = 1) -> pl.DataFrame:
    d0 = date(2020, 1, 1)
    return pl.DataFrame({
        "security_id": [sid] * len(closes),
        "session_date": [d0 + timedelta(days=i) for i in range(len(closes))],
        "close": closes,
    })


def test_simple_return_known_values():
    df = _frame([100.0, 110.0, 99.0])
    out = simple_return(df, period=1)["return_1d"].to_list()
    assert out[0] is None
    assert out[1] == pytest.approx(0.10)          # 110/100 - 1
    assert out[2] == pytest.approx(99/110 - 1)


def test_log_return_known_values():
    df = _frame([100.0, 110.0])
    out = log_return(df, period=1)["log_return_1d"].to_list()
    assert out[0] is None
    assert out[1] == pytest.approx(math.log(110/100))


def test_sma_known_value_on_increasing_sequence():
    # SMA of 1..20 over window 20 = mean(1..20) = 10.5, at the 20th row only.
    df = _frame([float(i) for i in range(1, 21)])
    out = sma(df, window=20)["sma20"].to_list()
    assert all(v is None for v in out[:19])       # warm-up: first 19 null
    assert out[19] == pytest.approx(10.5)         # mean(1..20)


def test_sma_window_slides():
    df = _frame([float(i) for i in range(1, 22)])  # 1..21
    out = sma(df, window=20)["sma20"].to_list()
    assert out[19] == pytest.approx(10.5)          # mean(1..20)
    assert out[20] == pytest.approx(11.5)          # mean(2..21)


def test_realized_vol_null_during_warmup_and_positive_after():
    # need window+1 prices; constant prices -> zero vol (all log returns 0)
    df = _frame([100.0] * 25)
    out = realized_vol(df, window=20)["realized_vol20"].to_list()
    assert out[19] is None                          # not enough log returns yet
    assert out[20] == pytest.approx(0.0)            # constant -> zero vol
    # a varying series -> strictly positive vol
    df2 = _frame([100.0, 101.0, 99.0, 102.0, 98.0] * 5)
    v = realized_vol(df2, window=20)["realized_vol20"].to_list()[-1]
    assert v is not None and v > 0


def test_roc_known_value():
    df = _frame([float(i) for i in range(1, 25)])   # 1..24
    out = roc(df, window=20)["roc20"].to_list()
    assert all(v is None for v in out[:20])
    assert out[20] == pytest.approx(21/1 - 1)        # close[20]=21 / close[0]=1 - 1


def test_momentum_12_1_definition():
    # 260 sessions; momentum at last row = close[-22]/close[-253] - 1
    closes = [float(i) for i in range(1, 261)]      # 1..260
    out = momentum_12_1(_frame(closes))["momentum_12_1"].to_list()
    assert all(v is None for v in out[:252])         # null until 252 prior sessions
    # at row 252 (the 253rd): close[252-21]/close[252-252] - 1 = close[231]/close[0] - 1
    expected = closes[231] / closes[0] - 1.0
    assert out[252] == pytest.approx(expected)


def test_functions_do_not_mutate_input():
    df = _frame([100.0, 101.0, 102.0])
    before = df.clone()
    _ = sma(df, window=2)
    assert df.equals(before)                         # input unchanged


def test_determinism_repeated_calls_identical():
    df = _frame([float(i) for i in range(1, 60)])
    a = realized_vol(df, window=20)
    b = realized_vol(df, window=20)
    assert a.equals(b)