"""Typed shapes for the calendar connector (prevents interface drift)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class ExchangeMeta:
    """Reference metadata for one exchange (ref.exchange)."""
    mic: str
    name: str
    country: str | None
    timezone: str


@dataclass(frozen=True)
class SessionRow:
    """
    One trading session (ref.trading_session). is_half_day comes from the
    library's early_closes set, which is DST-safe (early close is 1:00 PM LOCAL
    in both EST and EDT, i.e. a different UTC instant in each).
    """
    session_date: date
    open_utc: datetime
    close_utc: datetime
    is_half_day: bool