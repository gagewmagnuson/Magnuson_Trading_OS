"""
Canonical internal shapes for the EOD bars dataset.

Repo path: src/trading_os/bars/models.py

`Bar` is the source-independent representation every bars connector produces and
the shared writer (bars/writer.py) consumes. It is deliberately free of any
vendor parsing artifacts — a bar is a bar regardless of whether Alpaca, Tiingo,
or a silver rebuild produced it (DEC-024: bars semantics are dataset-scoped, not
source-scoped). Connector-specific shapes (bronze refs, raw payloads) stay in
their own connector's models.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


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