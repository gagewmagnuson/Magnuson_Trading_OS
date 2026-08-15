"""
Gold features router — GET /v1/features.

Repo path: src/trading_os/api/routers/gold.py

A thin translation layer over existing Trading OS capabilities:
auth -> optional resolve_ticker (DEC-002) -> gold_features_asof (PIT read) -> serialize.

No business logic: features are computed by the gold refresh, never here. This
endpoint reads the already-computed gold lake with knowledge_time <= as_of and
returns the full feature row per (security_id, session_date).

Symbols are optional — omit to pull the whole knowable universe (the snapshot-pull
case), or pass a subset (a dev slice). Requested symbols that do not resolve as of
the cutoff are NOT silently dropped: they are returned in `unresolved` so the caller
can judge completeness (the producer is transparent; the consumer holds the verdict).
Pure Parquet; no Postgres attach for the read.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading_os.api.deps import Consumer, get_conn, get_store, require_consumer
from trading_os.api.models import GoldFeatureRow, GoldFeaturesResponse
from trading_os.engine.store import DuckDBStore

router = APIRouter(tags=["features"])


@router.get("/v1/features", response_model=GoldFeaturesResponse)
def get_features(
    as_of: date | None = Query(
        default=None,
        description="Knowledge cutoff (end-of-day UTC). Omit for latest known; "
                    "pin for reproducible queries.",
    ),
    start: date | None = Query(default=None, description="Inclusive session_date lower bound."),
    end: date | None = Query(default=None, description="Inclusive session_date upper bound."),
    symbols: list[str] | None = Query(
        default=None,
        description="Symbols to include (repeatable). Omit for the whole knowable "
                    "universe. Symbols that do not resolve as of the cutoff are "
                    "returned in `unresolved`, never silently dropped.",
    ),
    consumer: Consumer = Depends(require_consumer),
    conn: psycopg.Connection = Depends(get_conn),
    store: DuckDBStore = Depends(get_store),
) -> GoldFeaturesResponse:
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start must be <= end.",
        )

    effective_as_of = as_of or datetime.now(timezone.utc).date()

    # Resolve requested symbols -> security_ids PIT (DEC-002). Unresolved symbols
    # are recorded and reported, not skipped: the caller must be able to see that
    # its request was not fully satisfied. None -> whole knowable universe.
    security_ids: list[int] | None = None
    resolved_symbols: list[str] | None = None
    unresolved: list[str] = []
    if symbols:
        security_ids = []
        resolved_symbols = []
        for sym in symbols:
            sid = conn.execute(
                "SELECT sec.resolve_ticker(%s, %s)", [sym, effective_as_of]
            ).fetchone()[0]
            if sid is None:
                unresolved.append(sym.upper())
            else:
                security_ids.append(sid)
                resolved_symbols.append(sym.upper())
        if not security_ids:
            # Nothing resolved: return empty rows, but report every unresolved
            # symbol so the caller's completeness check can fire.
            return GoldFeaturesResponse(
                as_of=effective_as_of, start=start, end=end,
                symbols=resolved_symbols, unresolved=unresolved, count=0, rows=[],
            )

    store.connect(attach_postgres=False)  # gold read is pure Parquet
    rows = store.gold_features_asof(
        effective_as_of, security_ids=security_ids, start=start, end=end,
    )

    # Tuple order (gold_features_asof):
    #   (security_id, symbol, session_date, knowledge_time, adj_close, adj_volume,
    #    return_1d, log_return_1d, sma20, sma50, ema20, realized_vol20, roc20, momentum_12_1)
    out = [
        GoldFeatureRow(
            session_date=r[2],
            knowledge_time=r[3],
            adj_close=r[4], adj_volume=r[5],
            return_1d=r[6], log_return_1d=r[7],
            sma20=r[8], sma50=r[9], ema20=r[10],
            realized_vol20=r[11], roc20=r[12], momentum_12_1=r[13],
        )
        for r in rows
    ]

    return GoldFeaturesResponse(
        as_of=effective_as_of, start=start, end=end,
        symbols=resolved_symbols, unresolved=unresolved, count=len(out), rows=out,
    )


@router.get("/v1/features/by-id/{security_id}", response_model=GoldFeaturesResponse)
def get_features_by_id(
    security_id: int,
    as_of: date | None = Query(
        default=None,
        description="Knowledge cutoff (end-of-day UTC). Omit for latest known; "
                    "pin for reproducible queries.",
    ),
    start: date | None = Query(default=None, description="Inclusive session_date lower bound."),
    end: date | None = Query(default=None, description="Inclusive session_date upper bound."),
    consumer: Consumer = Depends(require_consumer),
    conn: psycopg.Connection = Depends(get_conn),
    store: DuckDBStore = Depends(get_store),
) -> GoldFeaturesResponse:
    """Gold features by STABLE security_id — the identity path. Skips ticker
    resolution, so it retrieves DELISTED securities at a current knowledge cutoff
    (whose historical ticker no longer resolves) and is immune to ticker reuse.
    The read is identical to the symbol route's gold_features_asof."""
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start must be <= end.",
        )
    effective_as_of = as_of or datetime.now(timezone.utc).date()

    exists = conn.execute(
        "SELECT 1 FROM sec.security WHERE security_id = %s", [security_id]
    ).fetchone()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"security_id {security_id} not found.",
        )

    store.connect(attach_postgres=False)   # gold read is pure Parquet
    rows = store.gold_features_asof(
        effective_as_of, security_ids=[security_id], start=start, end=end,
    )
    out = [
        GoldFeatureRow(
            session_date=r[2], knowledge_time=r[3],
            adj_close=r[4], adj_volume=r[5],
            return_1d=r[6], log_return_1d=r[7],
            sma20=r[8], sma50=r[9], ema20=r[10],
            realized_vol20=r[11], roc20=r[12], momentum_12_1=r[13],
        )
        for r in rows
    ]
    return GoldFeaturesResponse(
        as_of=effective_as_of, start=start, end=end,
        symbols=None, unresolved=[], count=len(out), rows=out,
    )