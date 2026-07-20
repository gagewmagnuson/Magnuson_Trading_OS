"""
Tests for the DEC-024 Rule 1 knowledge_time primitive.

Pure-function tests (no DB, no network) — the kind the suite can eventually run
hermetically in CI. Assert the honest availability moment: normal close, half-day
early close, DST boundary, and strictness on non-session dates.
"""
from __future__ import annotations

from datetime import date, timezone

import pytest

from trading_os.bars.knowledge_time import (
    NonSessionDateError,
    market_close_knowledge_time,
)


def test_normal_close_is_2000_utc_in_winter():
    """A standard winter session closes 16:00 EST = 21:00 UTC."""
    kt = market_close_knowledge_time(date(2020, 1, 3))
    assert kt.tzinfo is not None
    assert kt.astimezone(timezone.utc).hour == 21
    assert kt.date() == date(2020, 1, 3)


def test_normal_close_is_2000_utc_in_summer():
    """A standard summer session closes 16:00 EDT = 20:00 UTC (DST handled by lib)."""
    kt = market_close_knowledge_time(date(2020, 7, 1))
    assert kt.astimezone(timezone.utc).hour == 20


def test_half_day_early_close():
    """Day after Thanksgiving 2019 closed 13:00 EST = 18:00 UTC, not 21:00.
    Proves half-days are honored (not hardcoded 16:00 ET)."""
    kt = market_close_knowledge_time(date(2019, 11, 29))
    assert kt.astimezone(timezone.utc).hour == 18


def test_weekend_raises():
    with pytest.raises(NonSessionDateError):
        market_close_knowledge_time(date(2020, 1, 4))  # Saturday


def test_holiday_raises():
    with pytest.raises(NonSessionDateError):
        market_close_knowledge_time(date(2020, 12, 25))  # Christmas


def test_knowledge_time_is_deterministic():
    """Same input -> same output, always (a pinned-calendar reproducibility guard)."""
    d = date(2018, 6, 15)
    assert market_close_knowledge_time(d) == market_close_knowledge_time(d)


def test_deep_historical_session_resolves():
    """A 1999 session must resolve, not raise DateOutOfBounds — the Tiingo-depth
    case that the default ~2006 calendar start missed."""
    kt = market_close_knowledge_time(date(1999, 11, 18))  # a Thursday, real session
    assert kt.date() == date(1999, 11, 18)
    assert kt.astimezone(timezone.utc).hour == 21  # Nov = EST, 16:00 -> 21:00 UTC


def test_very_old_session_resolves():
    """A 1962 session must resolve — the deep-history case that undershot the
    initial 1970 floor. The calendar must cover the full range of real equity dates."""
    kt = market_close_knowledge_time(date(1962, 1, 2))
    assert kt.date() == date(1962, 1, 2)