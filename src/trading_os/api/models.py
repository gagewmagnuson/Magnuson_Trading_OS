"""
Response models for the serving API — the permanent JSON contract.

Repo path: src/trading_os/api/models.py

These Pydantic models define the wire format every consumer builds against, so
a change here is a contract change. Identity (symbol, security_id) lives on the
envelope; each bar is data-only, so identity is not repeated on every row.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
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


class PeriodType(str, Enum):
    """Selects the FLOW duration to return; instant (balance-sheet) concepts are
    included alongside flows to form a complete financial snapshot (DEC-014).

    annual    -- annual-duration flows (period_end - period_start in 350..380 days)
                 PLUS all instant concepts  -> a complete annual snapshot
    quarterly -- quarterly-duration flows (85..95 days) PLUS all instant concepts
    instant   -- instant concepts ONLY (balance sheet alone)

    Instants have no duration (period_start IS NULL), so they cannot participate
    in a duration-mixing bug: including them never mixes annual with quarterly
    flows, which is precisely what DEC-014 exists to prevent. Durations are
    derived from actual dates, never from the unreliable `fiscal_period` label.

    YTD facts (6/9-month durations) exist in the store but are NOT served: DEC-014
    defines no canonical YTD band, and inventing one here would contradict a frozen
    decision. Exposing YTD requires a DEC-014 amendment first.

    # future: a separate `view`/`include_instants` axis could decouple "which flow
    # duration" from "do instants come along" (the Bloomberg/FactSet snapshot model).
    # Deliberately not built in V1 -- it is a new architectural axis, not a gap.
    """
    annual = "annual"
    quarterly = "quarterly"
    instant = "instant"


class FundamentalFact(BaseModel):
    """One point-in-time fundamental fact, data-only (identity is on the envelope)."""
    concept: str = Field(description="Canonical concept name (e.g. 'revenue', 'total_assets').")
    statement: str = Field(description="income | balance | cashflow | other.")
    period_start: date | None = Field(
        default=None,
        description="Start of the period a flow covers. Null for instant concepts.",
    )
    period_end_date: date = Field(
        description="Period end (flows) or the as-at date (instants)."
    )
    duration_days: int | None = Field(
        default=None,
        description="period_end_date - period_start, in days. Null for instants. "
                    "Derived from actual dates, never from fiscal_period (DEC-014).",
    )
    fiscal_period: str | None = Field(
        default=None,
        description="Vendor's fiscal-period label (FY/Q1/...). Informational only; "
                    "unreliable, and never used to determine duration.",
    )
    value: Decimal = Field(
        description="Exact numeric value, serialized as a JSON string to preserve "
                    "database precision (stored as numeric, never float)."
    )
    unit: str = Field(description="Unit of the value (e.g. 'USD', 'shares').")
    knowledge_time: datetime = Field(
        description="Filing acceptance time (filed_at) -- when this fact first became "
                    "knowable. Every returned row satisfies knowledge_time <= as_of."
    )


class FundamentalsResponse(BaseModel):
    """Envelope for GET /v1/fundamentals/{symbol}. Facts are ordered by
    (concept, period_end_date) ascending."""
    symbol: str
    security_id: int
    as_of: date = Field(
        description="Knowledge cutoff (end-of-day UTC). Omit for latest known; "
                    "pin for reproducible queries."
    )
    period_type: PeriodType = Field(
        description="The resolved period_type actually applied (echoed, so the caller "
                    "always knows which snapshot they received)."
    )
    concept: str | None = Field(default=None, description="Concept filter, if given.")
    statement: str | None = Field(default=None, description="Statement filter, if given.")
    count: int = Field(description="Number of facts returned.")
    facts: list[FundamentalFact]


class MacroObservation(BaseModel):
    """One point-in-time macro observation, data-only (series identity is on the envelope)."""
    obs_date: date = Field(
        description="Event-time: the period this value refers to (e.g. the quarter for GDP)."
    )
    value: Decimal | None = Field(
        default=None,
        description="Exact value, serialized as a JSON string to preserve numeric precision. "
                    "NULL is legitimate data: FRED publishes missing observations.",
    )
    vintage_date: date = Field(
        description="Knowledge_time: the date this value was published or revised (DEC-005). "
                    "Every returned row satisfies vintage_date <= as_of. Revisable series "
                    "(GDP, CPI, payrolls) have MANY vintages per obs_date — the value as first "
                    "released differs from the value as later revised, and this endpoint returns "
                    "the one knowable at as_of. Non-revisable market series (Treasury yields, "
                    "spreads) have exactly one vintage, where vintage_date = obs_date (DEC-015). "
                    "Note: a vintage_date of 1776-07-04 is ALFRED's sentinel for 'first/only known "
                    "vintage', retained as a genuine first-known marker — not a literal "
                    "publication date."
    )


class MacroResponse(BaseModel):
    """Envelope for GET /v1/macro/{series}. Observations ascend by obs_date."""
    series_id: str = Field(description="Canonical FRED series id (e.g. 'GDPC1'), upper-cased.")
    title: str = Field(description="Human-readable series title.")
    units: str | None = None
    frequency: str | None = Field(default=None, description="D | W | M | Q | A.")
    seasonal_adj: str | None = None
    as_of: date = Field(
        description="Knowledge cutoff (end-of-day UTC). Omit for latest known; "
                    "pin for reproducible queries."
    )
    start: date | None = Field(default=None, description="Inclusive obs_date lower bound, if given.")
    end: date | None = Field(default=None, description="Inclusive obs_date upper bound, if given.")
    count: int = Field(description="Number of observations returned.")
    observations: list[MacroObservation]