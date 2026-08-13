"""
Universe membership router — GET /v1/universe/{index}.

Repo path: src/trading_os/api/routers/universe.py

A thin translation layer over the authoritative PIT read function
univ.members_asof (DEC-027: event-time only). Membership is 'who was in the
index on the as_of date' by valid_from/valid_to containment — NOT knowledge_time
gated, because reconstructed index membership has load-time knowledge_time and a
knowledge_time gate would empty every historical query (DEC-027).

No membership logic here: the endpoint calls the stored function and resolves
each member's PIT-valid ticker (sec.security_identifier), the same PIT symbol
resolution Gold uses. Pure Postgres; no lake read.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading_os.api.deps import Consumer, get_conn, require_consumer
from trading_os.api.models import UniverseMemberRow, UniverseResponse

router = APIRouter(tags=["universe"])


@router.get("/v1/universe/{index}", response_model=UniverseResponse)
def get_universe(
    index: str,
    as_of: date | None = Query(
        default=None,
        description="Membership date (event-time). Omit for latest known membership; "
                    "pin for reproducible queries.",
    ),
    consumer: Consumer = Depends(require_consumer),
    conn: psycopg.Connection = Depends(get_conn),
) -> UniverseResponse:
    effective_as_of = as_of or datetime.now(timezone.utc).date()

    # Distinguish "index does not exist" (404) from "index exists, no members on
    # that date" (200, empty). The unique code is the client-facing identifier.
    exists = conn.execute(
        "select 1 from univ.universe where code = %s", [index]
    ).fetchone()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"universe '{index}' not found.",
        )

    # Authoritative PIT membership (DEC-027, event-time only), then PIT ticker
    # for each member (valid on as_of). One query, left join to keep members
    # even if no ticker interval covers the date.
    rows = conn.execute(
        """
        select m.security_id,
               (select si.id_value
                  from sec.security_identifier si
                 where si.security_id = m.security_id
                   and si.id_type = 'TICKER'
                   and si.valid_from <= %(as_of)s
                   and (si.valid_to is null or si.valid_to > %(as_of)s)
                 order by si.valid_from desc
                 limit 1) as symbol
          from univ.members_asof(%(code)s, %(as_of)s) m
         order by m.security_id
        """,
        {"code": index, "as_of": effective_as_of},
    ).fetchall()

    members = [UniverseMemberRow(security_id=r[0], symbol=r[1]) for r in rows]
    return UniverseResponse(
        index=index, as_of=effective_as_of, count=len(members), members=members,
    )