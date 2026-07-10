"""
Response models for the serving API — the permanent JSON contract.

Repo path: src/trading_os/api/models.py

These Pydantic models define the wire format every consumer builds against, so
a change here is a contract change. Identity (symbol, security_id) lives on the
envelope; each bar is data-only, so identity is not repeated on every row.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Adjustment(str, Enum):
    """Price-adjustment mode, applied ON READ (DEC-004); raw storage is never
    mutated. `none` -> raw stored prices; `split` -> split-adjusted (prices and
    volume, continuous); `total_return` -> split + cash-dividend adjusted. Only
    corporate actions known by `as_of` are applied, so the adjustment is itself
    lookahead-free. A value outside this set is rejected as 422 by FastAPI."""
    none = "none"
    split = "split"
    total_return = "total_return"


class BarRow(BaseModel):
    """One EOD bar, data-only. Identity (symbol/security_id) is on the envelope.
    Prices reflect the requested `adjustment`."""
    session_date: date = Field(description="Exchange session the bar covers.")
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int | None = Field(
        default=None, description="Number of trades; null if the source omits it."
    )
    vwap: float | None = Field(
        default=None, description="Volume-weighted average price; null if unavailable."
    )
    knowledge_time: datetime = Field(
        description="When this row first became knowable (bitemporal knowledge_time). "
                    "Every returned row satisfies knowledge_time <= as_of — the PIT guarantee."
    )
    source: str = Field(description="Vendor/source the bar was ingested from.")


class BarsResponse(BaseModel):
    """Envelope for GET /v1/bars/{symbol}. `bars` are ascending by session_date."""
    symbol: str = Field(description="Resolved ticker (as matched, case-insensitive).")
    security_id: int = Field(description="Internal stable security id the ticker resolved to.")
    as_of: date = Field(
        description="Knowledge cutoff (end-of-day UTC). Every returned row was knowable "
                    "by this date. Omit on the request for the latest known data; pin it "
                    "for reproducible queries."
    )
    adjustment: Adjustment
    start: date | None = Field(default=None, description="Inclusive session_date lower bound, if given.")
    end: date | None = Field(default=None, description="Inclusive session_date upper bound, if given.")
    count: int = Field(description="Number of bars returned (length of `bars`).")
    bars: list[BarRow]