"""
Shared ingestion lineage for the bars dataset (DEC-003, DEC-017).

Repo path: src/trading_os/bars/lineage.py

Source-independent Postgres helpers every bars connector needs: register the
vendor's ref.data_source row, open/close a meta.ingest_batch for lineage, and
resolve tickers to security_ids (resolve-and-skip, DEC-017). These are INGESTION
concepts, not any one vendor's concepts, so they live here and are used by the
Tiingo connector, the silver rebuild, and (later) the minute-bars connector —
the same "shared dataset logic, per-connector acquisition" split as the writer
and knowledge_time primitives.

Vendor-specific facts (redistributability, base_url, license) are NOT hardcoded:
each connector passes a SourceSpec describing its own vendor.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg


@dataclass(frozen=True)
class SourceSpec:
    """A vendor's ref.data_source descriptor. Each connector owns its own."""
    name: str                     # e.g. 'TIINGO', 'ALPACA'
    kind: str                     # e.g. 'prices'
    is_redistributable: bool
    base_url: str
    license_notes: str


def ensure_source(conn: psycopg.Connection, spec: SourceSpec) -> int:
    """Return the source_id for spec.name, inserting the row if absent (idempotent)."""
    row = conn.execute(
        "select source_id from ref.data_source where name = %s", (spec.name,)
    ).fetchone()
    if row:
        return row[0]
    return conn.execute(
        """
        insert into ref.data_source
            (name, kind, is_redistributable, base_url, license_notes)
        values (%s, %s, %s, %s, %s)
        returning source_id
        """,
        (spec.name, spec.kind, spec.is_redistributable, spec.base_url, spec.license_notes),
    ).fetchone()[0]


def open_batch(
    conn: psycopg.Connection,
    source_id: int,
    knowledge_time: datetime,
    params: dict,
    code_version: str,
    dataset: str = "bars_eod",
) -> int:
    """Open a meta.ingest_batch row (status 'running'); return its batch_id.

    knowledge_time here is the BATCH lineage timestamp (when this system fetched),
    distinct from each bar's per-session knowledge_time (DEC-024). code_version
    identifies the producing connector (e.g. 'tiingo-bars-v1').
    """
    return conn.execute(
        """
        insert into meta.ingest_batch
            (source_id, dataset, knowledge_time, params, code_version, status)
        values (%s, %s, %s, %s, %s, 'running')
        returning batch_id
        """,
        (source_id, dataset, knowledge_time, psycopg.types.json.Json(params), code_version),
    ).fetchone()[0]


def close_batch(
    conn: psycopg.Connection,
    batch_id: int,
    status: str,
    rows_in: int,
    rows_out: int,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        update meta.ingest_batch set status=%s, finished_at=now(),
               rows_in=%s, rows_out=%s, error=%s where batch_id=%s
        """,
        (status, rows_in, rows_out, error, batch_id),
    )


def resolve_security_ids(conn: psycopg.Connection, symbols: list[str]) -> dict[str, int]:
    """Return {symbol: security_id} for symbols in the master; omit the rest
    (resolve-and-skip, DEC-017). Resolves as of current_date."""
    out: dict[str, int] = {}
    for s in symbols:
        r = conn.execute(
            "select sec.resolve_ticker(%s, current_date)", (s,)
        ).fetchone()
        if r and r[0] is not None:
            out[s] = r[0]
    return out