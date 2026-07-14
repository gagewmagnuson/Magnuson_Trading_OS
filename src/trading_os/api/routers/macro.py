"""
Macro router — GET /v1/macro/{series}.

Repo path: src/trading_os/api/routers/macro.py

A thin translation layer, same shape as bars and fundamentals, but simpler in one
way and richer in another.

Simpler: there is no ticker resolution. A macro series_id (e.g. 'GDPC1') IS the
natural key — no security_id, no resolve_ticker.

Richer: this endpoint carries the system's deepest PIT semantics. Macro statistics
are REVISED. GDP as released on 2020-04-29 is a different number from the GDP for
that same quarter as revised today. macro.observation stores every vintage
(DEC-005), and vintage_date is the knowledge_time. So:

    GET /v1/macro/GDPC1?as_of=2020-06-01   -> what GDP *said* in June 2020
    GET /v1/macro/GDPC1?as_of=2026-01-01   -> what we now believe about those
                                              same quarters

Same obs_date, different values, both correct. That is the lookahead-bias
protection ALFRED exists to provide, and most retail data stacks lack it.

Per DEC-015, revisable statistics and non-revisable market series (Treasury
yields, spreads — one vintage, vintage_date = obs_date) share one schema and one
as-of path. No `revisable` flag is exposed: "consumers neither know nor care."

IMPORTANT: reads Postgres macro.observations_asof — the system of record. It does
NOT use DuckDBStore.macro_value_asof, which reads a transient, non-authoritative
V0 proof artifact in the lake.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading_os.api.deps import Consumer, get_conn, require_consumer
from trading_os.api.models import MacroObservation, MacroResponse

router = APIRouter(tags=["macro"])


@router.get("/v1/macro/{series}", response_model=MacroResponse)
def get_macro(
    series: str,
    as_of: date | None = Query(
        default=None,
        description="Knowledge cutoff (end-of-day UTC). Omit for latest known; "
                    "pin for reproducible queries. Changing as_of changes which "
                    "VINTAGE of each observation you receive.",
    ),
    start: date | None = Query(default=None, description="Inclusive obs_date lower bound."),
    end: date | None = Query(default=None, description="Inclusive obs_date upper bound."),
    consumer: Consumer = Depends(require_consumer),
    conn: psycopg.Connection = Depends(get_conn),
) -> MacroResponse:
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start must be <= end.",
        )

    # Canonicalize the identifier ONCE, at the top; everything downstream (lookup,
    # filter, echo) uses this one form. Unlike bars/fundamentals — where
    # sec.resolve_ticker does case-insensitive matching in SQL — macro has no
    # resolver, so canonicalization happens here. Doing it once (rather than at
    # each use site) keeps the response echo canonical, which is what makes two
    # differently-cased but otherwise identical requests byte-identical (DEC-023).
    series_id = series.upper()

    effective_as_of = as_of or datetime.now(timezone.utc).date()

    meta = conn.execute(
        "SELECT series_id, title, units, frequency, seasonal_adj "
        "FROM macro.series WHERE series_id = %s",
        [series_id],
    ).fetchone()
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"macro series {series_id!r} not found.",
        )

    # Build predicates and their params in one pass, so placeholder order is
    # correct by construction.
    sql = """
        SELECT o.obs_date, o.value, o.vintage_date
          FROM macro.observations_asof(%s::date) o
         WHERE o.series_id = %s
    """
    params: list[object] = [effective_as_of, series_id]

    if start is not None:
        sql += " AND o.obs_date >= %s"
        params.append(start)
    if end is not None:
        sql += " AND o.obs_date <= %s"
        params.append(end)

    sql += " ORDER BY o.obs_date"

    rows = conn.execute(sql, params).fetchall()

    observations = [
        MacroObservation(obs_date=r[0], value=r[1], vintage_date=r[2]) for r in rows
    ]
    return MacroResponse(
        series_id=meta[0],          # canonical form from the DB, not the request
        title=meta[1],
        units=meta[2],
        frequency=meta[3],
        seasonal_adj=meta[4],
        as_of=effective_as_of,
        start=start,
        end=end,
        count=len(observations),
        observations=observations,
    )