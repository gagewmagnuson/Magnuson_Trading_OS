"""
Bars router — GET /v1/bars/{symbol}.

Repo path: src/trading_os/api/routers/bars.py

A thin translation layer over existing Trading OS capabilities:
    auth -> resolve_ticker (DEC-002) -> bars_eod_asof (PIT + on-read adjustment) -> serialize.

No business logic lives here: the adjustment is a parameter INTO the read path,
never computed here, and the endpoint neither mutates nor transforms data. The
Postgres attach is taken only when an adjustment actually needs corporate actions.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading_os.api.deps import Consumer, get_conn, get_store, require_consumer
from trading_os.api.models import Adjustment, BarRow, BarsResponse
from trading_os.engine.store import DuckDBStore

router = APIRouter(tags=["bars"])


@router.get("/v1/bars/{symbol}", response_model=BarsResponse)
def get_bars(
    symbol: str,
    as_of: date | None = Query(
        default=None,
        description="Knowledge cutoff (end-of-day UTC). Omit for latest known; "
                    "pin for reproducible queries.",
    ),
    start: date | None = Query(default=None, description="Inclusive session_date lower bound."),
    end: date | None = Query(default=None, description="Inclusive session_date upper bound."),
    adjustment: Adjustment = Query(default=Adjustment.none, description="On-read price adjustment."),
    consumer: Consumer = Depends(require_consumer),
    conn: psycopg.Connection = Depends(get_conn),
    store: DuckDBStore = Depends(get_store),
) -> BarsResponse:
    # Validate the range up front — a clear 422 beats a confusing empty result.
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start must be <= end.",
        )

    # Default as_of to 'now' (latest known), expressed as today's UTC date.
    effective_as_of = as_of or datetime.now(timezone.utc).date()

    # Resolve ticker -> security_id, PIT as of the knowledge date (DEC-002).
    # resolve_ticker returns NULL when nothing resolves as of that date -> 404.
    security_id = conn.execute(
        "SELECT sec.resolve_ticker(%s, %s)", [symbol, effective_as_of]
    ).fetchone()[0]
    if security_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"symbol {symbol.upper()!r} not found as of {effective_as_of.isoformat()}.",
        )

    # Attach Postgres only when the adjustment needs corporate actions; the raw
    # (unadjusted) path is pure Parquet. connect() is always required (it opens
    # the in-memory DuckDB that reads the lake).
    store.connect(attach_postgres=(adjustment != Adjustment.none))
    adj_arg = None if adjustment == Adjustment.none else adjustment.value
    rows = store.bars_eod_asof(
        effective_as_of,
        security_ids=[security_id],
        start=start,
        end=end,
        adjustment=adj_arg,
    )

    # Tuple order: (security_id, symbol, session_date, open, high, low, close,
    #               volume, trade_count, vwap, knowledge_time, source).
    # Identity (0,1) is on the envelope; each bar carries indices 2..11.
    bars = [
        BarRow(
            session_date=r[2],
            open=r[3], high=r[4], low=r[5], close=r[6],
            volume=r[7], trade_count=r[8], vwap=r[9],
            knowledge_time=r[10], source=r[11],
        )
        for r in rows
    ]
    return BarsResponse(
        symbol=symbol.upper(),
        security_id=security_id,
        as_of=effective_as_of,
        adjustment=adjustment,
        start=start,
        end=end,
        count=len(bars),
        bars=bars,
    )