"""Typed shapes for the Alpaca bars connector."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Bar:
    """One unadjusted daily bar, resolved to a security_id."""
    security_id: int
    symbol: str
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int | None
    vwap: float | None


@dataclass(frozen=True)
class BronzeRef:
    path: str
    downloaded_at: datetime