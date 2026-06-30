"""
CAF adjustment-engine tests. Pure: hand-built actions, no DB, no vendor.
Proves single split, multiple splits, reverse split, dividends, combinations,
volume direction, and PIT correctness (an action unknown/after as_of must not
bend earlier history — enforced here by the caller filtering the action set,
exactly as the engine layer will).
"""
from __future__ import annotations

from datetime import date

import pytest

from trading_os.engine.adjust import (
    Action,
    compute_adjustment_factors,
)

SID = 1


def _sessions(*days):
    return {SID: [date(2020, 1, d) for d in days]}


def _closes(mapping):
    return {SID: {date(2020, 1, d): c for d, c in mapping.items()}}


# ----------------------------- splits -----------------------------
def test_single_split_scales_pre_split_prices():
    # 4-for-1 split ex 2020-01-10. Pre-split bars get price *1/4, volume *4.
    actions = [Action(SID, "SPLIT", date(2020, 1, 10), split_from=1, split_to=4)]
    fac = compute_adjustment_factors(actions, {}, _sessions(8, 10, 12), mode="split")
    assert fac[(SID, date(2020, 1, 8))].price_factor == pytest.approx(0.25)
    assert fac[(SID, date(2020, 1, 8))].volume_factor == pytest.approx(4.0)
    # On/after the ex-date: unadjusted (factor 1).
    assert fac[(SID, date(2020, 1, 10))].price_factor == pytest.approx(1.0)
    assert fac[(SID, date(2020, 1, 12))].price_factor == pytest.approx(1.0)


def test_reverse_split():
    # 1-for-8 reverse split: split_from=8, split_to=1 -> price *8, volume *1/8.
    actions = [Action(SID, "SPLIT", date(2020, 1, 10), split_from=8, split_to=1)]
    fac = compute_adjustment_factors(actions, {}, _sessions(8, 10), mode="split")
    assert fac[(SID, date(2020, 1, 8))].price_factor == pytest.approx(8.0)
    assert fac[(SID, date(2020, 1, 8))].volume_factor == pytest.approx(0.125)


def test_multiple_splits_compose_as_product():
    # 2-for-1 ex 01-05, then 3-for-1 ex 01-10. A bar before BOTH gets 1/2 * 1/3.
    actions = [
        Action(SID, "SPLIT", date(2020, 1, 5), split_from=1, split_to=2),
        Action(SID, "SPLIT", date(2020, 1, 10), split_from=1, split_to=3),
    ]
    fac = compute_adjustment_factors(actions, {}, _sessions(3, 7, 12), mode="split")
    assert fac[(SID, date(2020, 1, 3))].price_factor == pytest.approx(1/6)   # before both
    assert fac[(SID, date(2020, 1, 7))].price_factor == pytest.approx(1/3)   # between
    assert fac[(SID, date(2020, 1, 12))].price_factor == pytest.approx(1.0)  # after both


# ----------------------------- dividends -----------------------------
def test_dividend_total_return_uses_day_before_ex_close():
    # $2 dividend ex 01-10; close on 01-09 (day before ex) is 100 -> f = 0.98.
    actions = [Action(SID, "CASH_DIVIDEND", date(2020, 1, 10), cash_amount=2.0)]
    closes = _closes({8: 100.0, 9: 100.0, 10: 98.0})
    fac = compute_adjustment_factors(actions, closes, _sessions(8, 9, 10),
                                     mode="total_return")
    assert fac[(SID, date(2020, 1, 9))].price_factor == pytest.approx(0.98)
    assert fac[(SID, date(2020, 1, 10))].price_factor == pytest.approx(1.0)
    # dividends never move volume
    assert fac[(SID, date(2020, 1, 9))].volume_factor == pytest.approx(1.0)


def test_dividend_ignored_in_split_mode():
    actions = [Action(SID, "CASH_DIVIDEND", date(2020, 1, 10), cash_amount=2.0)]
    closes = _closes({9: 100.0})
    fac = compute_adjustment_factors(actions, closes, _sessions(9, 10), mode="split")
    assert fac[(SID, date(2020, 1, 9))].price_factor == pytest.approx(1.0)


def test_split_and_dividend_combination():
    # dividend $1 ex 01-05 (prev close 100 -> 0.99), then 2-for-1 ex 01-10 (0.5).
    # A bar before both: 0.99 * 0.5 = 0.495. Volume: only the split -> *2.
    actions = [
        Action(SID, "CASH_DIVIDEND", date(2020, 1, 5), cash_amount=1.0),
        Action(SID, "SPLIT", date(2020, 1, 10), split_from=1, split_to=2),
    ]
    closes = _closes({3: 100.0, 4: 100.0})
    fac = compute_adjustment_factors(actions, closes, _sessions(3, 4, 7, 12),
                                     mode="total_return")
    assert fac[(SID, date(2020, 1, 3))].price_factor == pytest.approx(0.495)
    assert fac[(SID, date(2020, 1, 3))].volume_factor == pytest.approx(2.0)
    assert fac[(SID, date(2020, 1, 7))].price_factor == pytest.approx(0.5)   # after div only


# ----------------------------- modes / PIT -----------------------------
def test_mode_none_is_identity():
    actions = [Action(SID, "SPLIT", date(2020, 1, 10), split_from=1, split_to=4)]
    fac = compute_adjustment_factors(actions, {}, _sessions(8, 12), mode=None)
    assert fac[(SID, date(2020, 1, 8))].price_factor == 1.0
    assert fac[(SID, date(2020, 1, 8))].volume_factor == 1.0


def test_pit_action_filtered_out_does_not_adjust():
    # Caller filters actions by as_of. If the split isn't in the set (not yet
    # known/occurred as-of the query date), history is unadjusted.
    fac = compute_adjustment_factors([], {}, _sessions(8, 12), mode="split")
    assert fac[(SID, date(2020, 1, 8))].price_factor == 1.0


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        compute_adjustment_factors([], {}, _sessions(8), mode="bogus")


# ===================== Layer 2: application to bar tuples (hermetic) =====================
from datetime import datetime, timezone  # noqa: E402

from trading_os.engine.adjust import apply_factors_to_bars  # noqa: E402

_KT = datetime(2020, 1, 1, tzinfo=timezone.utc)  # placeholder; apply passes it through


def _bar(sid, sym, d, o, h, l, c, vol):
    # (security_id, symbol, session_date, open, high, low, close, volume,
    #  trade_count, vwap, knowledge_time, source)
    return (sid, sym, d, o, h, l, c, vol, 100, c, _KT, "TEST")


def test_apply_split_eliminates_discontinuity_and_scales_volume():
    # Synthetic 4-for-1: pre-split close 400 (08-28), post-split 100 (08-31).
    bars = [
        _bar(1, "X", date(2020, 8, 28), 400, 400, 400, 400, 1000),
        _bar(1, "X", date(2020, 8, 31), 100, 100, 100, 100, 4000),
    ]
    actions = [Action(1, "SPLIT", date(2020, 8, 31), split_from=1, split_to=4)]
    sessions = {1: [date(2020, 8, 28), date(2020, 8, 31)]}
    factors = compute_adjustment_factors(actions, {}, sessions, mode="split")
    adj = {b[2]: b for b in apply_factors_to_bars(bars, factors)}

    # discontinuity gone: pre-split 400 -> 100, continuous with post-split 100
    assert adj[date(2020, 8, 28)][6] == pytest.approx(100.0)   # close
    assert adj[date(2020, 8, 31)][6] == pytest.approx(100.0)
    # volume: pre-split 1000 -> 4000 (×4); post-split unchanged
    assert adj[date(2020, 8, 28)][7] == 4000
    assert adj[date(2020, 8, 31)][7] == 4000
    # all OHLC of the pre-split bar scaled
    assert adj[date(2020, 8, 28)][3:7] == pytest.approx((100.0, 100.0, 100.0, 100.0))


def test_apply_recent_prices_unchanged():
    # The post-split (most recent) bar keeps its real values.
    bars = [_bar(1, "X", date(2020, 8, 31), 130, 131, 129, 130, 5000)]
    actions = [Action(1, "SPLIT", date(2020, 8, 31), split_from=1, split_to=4)]
    factors = compute_adjustment_factors(actions, {}, {1: [date(2020, 8, 31)]}, mode="split")
    adj = apply_factors_to_bars(bars, factors)[0]
    assert adj[3:8] == (130, 131, 129, 130, 5000)  # untouched


def test_apply_mode_none_returns_bars_unchanged():
    bars = [_bar(1, "X", date(2020, 8, 28), 400, 400, 400, 400, 1000)]
    factors = compute_adjustment_factors([], {}, {1: [date(2020, 8, 28)]}, mode=None)
    adj = apply_factors_to_bars(bars, factors)
    assert adj[0] == bars[0]  # identical tuple


def test_apply_total_return_scales_price_not_volume():
    # $2 dividend ex 01-10, prev close (01-09) = 100 -> price ×0.98, volume same.
    bars = [
        _bar(1, "X", date(2020, 1, 9), 100, 100, 100, 100, 1000),
        _bar(1, "X", date(2020, 1, 10), 98, 98, 98, 98, 1200),
    ]
    actions = [Action(1, "CASH_DIVIDEND", date(2020, 1, 10), cash_amount=2.0)]
    closes = {1: {date(2020, 1, 9): 100.0, date(2020, 1, 10): 98.0}}
    sessions = {1: [date(2020, 1, 9), date(2020, 1, 10)]}
    factors = compute_adjustment_factors(actions, closes, sessions, mode="total_return")
    adj = {b[2]: b for b in apply_factors_to_bars(bars, factors)}
    assert adj[date(2020, 1, 9)][6] == pytest.approx(98.0)   # 100 × 0.98
    assert adj[date(2020, 1, 9)][7] == 1000                  # volume unchanged by dividend