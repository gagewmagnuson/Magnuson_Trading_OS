"""
Fundamentals router — GET /v1/fundamentals/{symbol}.

Repo path: src/trading_os/api/routers/fundamentals.py

A thin translation layer, same shape as bars:
    auth -> resolve_ticker (DEC-002) -> fundamentals_asof (PIT) -> serialize.

Unlike bars, this endpoint is pure Postgres — no DuckDB store, no attach. It reads
fund.fundamentals_asof(as_of) joined to fund.concept, filtered to one security.

DEC-014 (duration identity) is enforced here: Company Facts reports 3-month, YTD and
12-month flows against the SAME period_end, so duration is part of a flow fact's
identity. Duration is derived from actual dates (period_end_date - period_start),
NEVER from the unreliable `fiscal_period` label. Bands, verbatim from DEC-014:
    annual    350..380 days
    quarterly  85..95  days
Instants carry period_start IS NULL. See models.PeriodType for the snapshot semantics
(flows of the selected duration, plus instants).

DEC-009: only research_status='core' concepts are served. Note ebitda/fcf are
computed-on-read derivations, not stored XBRL facts, so they never appear here —
by design, not a gap.

Filters intersect: an impossible combination (concept=revenue & statement=balance)
returns 200 with count=0, not an error. The API does not police semantics.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading_os.api.deps import Consumer, get_conn, require_consumer
from trading_os.api.models import FundamentalFact, FundamentalsResponse, PeriodType

router = APIRouter(tags=["fundamentals"])

# DEC-014 duration bands, in days, inclusive. Frozen values — do not tune.
DURATION_BANDS = {
    PeriodType.annual: (350, 380),
    PeriodType.quarterly: (85, 95),
}

BASE_SQL = """
    SELECT c.canonical_name,
           c.statement,
           f.period_start,
           f.period_end_date,
           CASE WHEN f.period_start IS NULL THEN NULL
                ELSE (f.period_end_date - f.period_start) END AS duration_days,
           f.fiscal_period,
           f.value,
           f.unit,
           f.filed_at
      FROM fund.fundamentals_asof(%s::timestamptz) f
      JOIN fund.concept c ON c.concept_id = f.concept_id
     WHERE f.security_id = %s
       AND c.research_status = 'core'
"""


@router.get("/v1/fundamentals/{symbol}", response_model=FundamentalsResponse)
def get_fundamentals(
    symbol: str,
    as_of: date | None = Query(
        default=None,
        description="Knowledge cutoff (end-of-day UTC). Omit for latest known; "
                    "pin for reproducible queries.",
    ),
    period_type: PeriodType = Query(
        default=PeriodType.annual,
        description="Flow duration to return; instants are included with annual and "
                    "quarterly to form a complete snapshot.",
    ),
    concept: str | None = Query(
        default=None, description="Filter to one canonical concept (e.g. 'revenue')."
    ),
    statement: str | None = Query(
        default=None, description="Filter to one statement: income|balance|cashflow|other."
    ),
    consumer: Consumer = Depends(require_consumer),
    conn: psycopg.Connection = Depends(get_conn),
) -> FundamentalsResponse:
    effective_as_of = as_of or datetime.now(timezone.utc).date()

    security_id = conn.execute(
        "SELECT sec.resolve_ticker(%s, %s)", [symbol, effective_as_of]
    ).fetchone()[0]
    if security_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"symbol {symbol.upper()!r} not found as of {effective_as_of.isoformat()}.",
        )

    # Build the query as (clause, params) pairs appended together, so each
    # placeholder's value is bound in the same pass that adds the placeholder.
    # Positional binding makes order the whole contract — never build the SQL and
    # the params in separate passes.
    sql = BASE_SQL
    params: list[object] = [effective_as_of, security_id]   # matches BASE_SQL's two %s

    if period_type == PeriodType.instant:
        sql += " AND f.period_start IS NULL"                # no params
    else:
        lo, hi = DURATION_BANDS[period_type]
        sql += (
            " AND (f.period_start IS NULL"                  # instants ride along
            "      OR (f.period_end_date - f.period_start) BETWEEN %s AND %s)"
        )
        params += [lo, hi]

    if concept is not None:
        sql += " AND c.canonical_name = %s"
        params.append(concept)

    if statement is not None:
        sql += " AND c.statement = %s"
        params.append(statement)

    sql += " ORDER BY c.canonical_name, f.period_end_date"

    rows = conn.execute(sql, params).fetchall()

    facts = [
        FundamentalFact(
            concept=r[0],
            statement=r[1],
            period_start=r[2],
            period_end_date=r[3],
            duration_days=r[4],
            fiscal_period=r[5],
            value=r[6],
            unit=r[7],
            knowledge_time=r[8],
        )
        for r in rows
    ]
    return FundamentalsResponse(
        symbol=symbol.upper(),
        security_id=security_id,
        as_of=effective_as_of,
        period_type=period_type,
        concept=concept,
        statement=statement,
        count=len(facts),
        facts=facts,
    )