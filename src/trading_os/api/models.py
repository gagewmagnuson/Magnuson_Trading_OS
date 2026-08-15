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


# ---------------------------------------------------------------------------
# Health + Catalog contract (V1 operational metadata surface).
#
# FROZEN CONTRACT: field names and semantics are STABLE. Future changes may only
# ADD optional fields or new endpoints — never rename or repurpose existing
# fields. Consumers (UI, Research OS, monitors, agents) depend on this shape.
#
# Freshness is reported as FACTS (last_capture, latest_event, expected_frequency,
# lag), never as an opinion ("stale"). Each consumer applies its own thresholds.
# ---------------------------------------------------------------------------

class PingResponse(BaseModel):
    status: str
    server_time: datetime
    version: str
    git_sha: str | None
    db_connected: bool


class SourceHealth(BaseModel):
    name: str                       # ref.data_source.name, e.g. 'FRED'
    dataset: str                    # the pipeline's dataset, e.g. 'bars_eod'
    kind: str                       # 'prices' | 'macro' | 'fundamentals' | 'reference'
    last_batch_at: datetime | None  # most recent succeeded ingest_batch for this pipeline
    last_status: str | None         # status of the most recent batch
    expected_frequency: str         # cadence (a fact)
    lag_seconds: int | None         # now - last_batch_at (fact; consumer judges)
    retired: bool                   # pipeline permanently decommissioned
    critical: bool                  # failure threatens downstream correctness


class JobRun(BaseModel):
    batch_id: int
    dataset: str
    source_id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    rows_in: int | None
    rows_out: int | None
    code_version: str | None
    error: str | None


class DQResultItem(BaseModel):
    result_id: int
    check_name: str
    batch_id: int | None
    run_at: datetime
    passed: bool
    severity: str | None            # from observed->>'severity'
    count: int | None               # from observed->>'count'
    details: str | None


class HealthSummary(BaseModel):
    generated_at: datetime
    sources: list[SourceHealth]
    recent_jobs: list[JobRun]       # last N batches
    recent_failures: list[JobRun]   # batches with status='failed'
    recent_dq_failures: list[DQResultItem]  # dq_result where passed=false


class FeatureDefinitionItem(BaseModel):
    name: str
    version: int
    description: str | None
    spec: dict | None
    inputs: list[str] | None
    pit_semantics: str | None
    code_ref: str | None


class DatasetItem(BaseModel):
    name: str                       # 'bars' | 'gold' | 'macro' | 'fundamentals' | 'universe'
    description: str
    storage: str                    # 'parquet_lake' | 'postgres'


class GoldFeatureRow(BaseModel):
    """One gold feature row, data-only. Identity (symbol/security_id) is on the
    envelope. Values are the PIT-correct derived features as known by `as_of`."""
    session_date: date = Field(description="Exchange session the features cover.")
    adj_close: float = Field(description="Split+dividend adjusted close the features were computed from.")
    adj_volume: int
    return_1d: float | None = Field(default=None, description="One-session simple return.")
    log_return_1d: float | None = Field(default=None, description="One-session log return.")
    sma20: float | None = Field(default=None, description="20-session simple moving average of adj_close.")
    sma50: float | None = Field(default=None, description="50-session simple moving average of adj_close.")
    ema20: float | None = Field(default=None, description="20-span EMA of adj_close.")
    realized_vol20: float | None = Field(default=None, description="20-session realized volatility.")
    roc20: float | None = Field(default=None, description="20-session rate of change.")
    momentum_12_1: float | None = Field(default=None, description="12-1 momentum (skip most recent month).")
    knowledge_time: datetime = Field(
        description="When this feature row first became knowable. Every returned row "
                    "satisfies knowledge_time <= as_of — the PIT guarantee."
    )


class GoldFeaturesResponse(BaseModel):
    """Envelope for GET /v1/features. Rows ascending by (security_id, session_date).
    Features are computed by the gold refresh; this endpoint only reads them PIT."""
    as_of: date = Field(
        description="Knowledge cutoff. Every returned row was knowable by this date. "
                    "Omit for latest known; pin for reproducible queries."
    )
    start: date | None = Field(default=None, description="Inclusive session_date lower bound, if given.")
    end: date | None = Field(default=None, description="Inclusive session_date upper bound, if given.")
    symbols: list[str] | None = Field(default=None, description="Resolved symbols filtered to, if given.")
    unresolved: list[str] = Field(
        default_factory=list,
        description="Requested symbols that did not resolve to a security as of the "
                    "cutoff. Never silently dropped: the caller sees exactly what was "
                    "not found and decides whether the result is complete. Empty when "
                    "all requested symbols resolved or no symbols were requested.",
    )
    count: int = Field(description="Number of rows returned.")
    rows: list[GoldFeatureRow]


class UniverseMemberRow(BaseModel):
    """One universe member as of the query date, data-only. Event-time semantics
    (DEC-027): membership is 'who was in the index on as_of', by valid_from/valid_to
    containment — NOT knowledge_time gated, because reconstructed membership has
    load-time knowledge_time."""
    security_id: int = Field(description="Stable internal security id (the safe join key).")
    symbol: str | None = Field(
        default=None,
        description="Ticker valid on the as_of date (PIT-resolved). Null if no ticker "
                    "interval covers the date.",
    )


class UniverseResponse(BaseModel):
    """Envelope for GET /v1/universe/{index}. Members as of `as_of`, event-time
    (DEC-027). Ascending by security_id."""
    index: str = Field(description="Universe code, e.g. 'SP500' (matches univ.universe.code).")
    as_of: date = Field(
        description="Membership date. Returns who was in the index on this date "
                    "(event-time containment). Omit for latest; pin for reproducibility."
    )
    count: int = Field(description="Number of members returned.")
    members: list[UniverseMemberRow]


class MembershipInterval(BaseModel):
    """One membership interval, event-time. Identity is security_id (the stable
    key); the membership table carries NO ticker — ticker is a separate PIT
    attribute, never the identity. valid_to is null for a currently-open interval."""
    security_id: int = Field(description="Stable security id — the identity for R1 to key on.")
    valid_from: date = Field(description="Event-time interval start (inclusive).")
    valid_to: date | None = Field(default=None, description="Event-time interval end (exclusive); null if open/current.")


class UniverseHistoryResponse(BaseModel):
    """Envelope for GET /v1/universe/{index}/history — the COMPLETE membership
    interval history (survivorship-free: includes securities long delisted). A
    security may appear in multiple intervals (left and re-entered). Consumers
    reconstruct PIT membership at any date by interval containment, locally."""
    index: str
    as_of: date = Field(
        description="Knowledge cutoff (knowledge_time <= as_of). Currently a no-op: "
                    "membership knowledge_time is uniform load-time (DEC-027), so the "
                    "filter excludes nothing today. Accepted and applied for bitemporal "
                    "correctness if membership gains real knowledge-time later."
    )
    interval_count: int = Field(description="Number of membership intervals returned.")
    security_count: int = Field(description="Number of DISTINCT securities across those intervals.")
    intervals: list[MembershipInterval]