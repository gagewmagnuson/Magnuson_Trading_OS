"""
Serving-API dependency facade — request-scoped resources every router shares:
a read-only Postgres connection (DEC-022) and API-key authentication (DEC-021).

Repo path: src/trading_os/api/deps.py

Routers depend on these via FastAPI's Depends(), never opening connections or
checking keys themselves, so the connection strategy and the auth mechanism each
live in exactly one place.

Connection strategy (DEC-022): one short-lived, READ-ONLY psycopg connection per
request, opened here and closed on teardown. No pool yet. Routers depend on
`get_conn` abstractly, so swapping in psycopg_pool later is contained to this file.

Authentication (DEC-021): the caller presents `Authorization: Bearer <key>`.
We SHA-256 the key and look it up in meta.api_consumer WHERE is_active. The raw
key is never stored and never logged. Absent, unknown, or inactive keys get 401.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import psycopg
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from trading_os.config import settings
from trading_os.engine.store import DuckDBStore


# --- Postgres: read-only, connection-per-request (DEC-022) ------------------

def get_conn():
    """Yield a request-scoped, READ-ONLY Postgres connection, closed on teardown.

    Read-only is defense-in-depth: the serving API must never write
    (ARCHITECTURE.md / DEC-022). An accidental write in any router fails at the
    database instead of mutating data. Key minting/revocation is a separate
    admin path on its own writable connection — never through the serving API.

    FastAPI caches this within a request, so auth and the route handler share
    ONE connection per request.
    """
    conn = psycopg.connect(settings.pg_conninfo())
    conn.autocommit = True   # read-only SELECTs; no lingering open transaction
    # Enforce read-only at the session level with explicit SQL. Postgres then
    # rejects any write for the life of the connection with SQLSTATE 25006.
    # (Setting conn.read_only as an attribute is order/state-sensitive under
    # autocommit and can silently no-op — DEC-022 read-only must be guaranteed,
    # so we assert it in SQL instead.)
    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    try:
        yield conn
    finally:
        conn.close()

# --- DuckDB store: per-request lifecycle, conditional attach (DEC-022) -------

def get_store():
    """Yield a request-scoped DuckDBStore, closed on teardown.

    Construction is cheap (no connection); the caller calls store.connect(...)
    itself and chooses whether to attach Postgres. Raw bar reads need only the
    Parquet lake (attach_postgres=False, ~10ms). Split/total_return adjustment
    reads reach into pg.corp.corporate_action and need attach_postgres=True.
    Attaching only when the query needs it keeps the common unadjusted path cheap.

    Per-request, not app-lifetime, by design: a DuckDB connection is not safe to
    share across FastAPI's threadpool workers, so each request owns its store.
    Mirrors the connection-per-request choice for Postgres (DEC-022). A future
    shared/pooled store is the same 'revisit under concurrency' trigger as the
    psycopg pool.
    """
    store = DuckDBStore()
    try:
        yield store
    finally:
        store.close()   # safe even if connect() was never called (guards con is None)

# --- Authentication (DEC-021) -----------------------------------------------

@dataclass(frozen=True)
class Consumer:
    """The authenticated API consumer behind a request."""
    consumer_id: int
    label: str


def hash_key(raw_key: str) -> str:
    """SHA-256 hex of an API key. The single place keys are hashed — shared by
    the auth check here and the admin key-minting CLI, so both always agree."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _lookup_consumer(conn: psycopg.Connection, raw_key: str) -> Consumer | None:
    """Resolve a raw API key to an active Consumer, or None. Pure DB lookup by
    key hash — no FastAPI machinery — so it is directly unit-testable."""
    row = conn.execute(
        "SELECT consumer_id, label FROM meta.api_consumer "
        "WHERE key_hash = %s AND is_active",
        [hash_key(raw_key)],
    ).fetchone()
    return Consumer(consumer_id=row[0], label=row[1]) if row else None


_bearer = HTTPBearer(auto_error=False)  # we raise 401 ourselves (see below)


def _unauthorized() -> HTTPException:
    # 401 for absent/unknown/inactive (DEC-021). HTTPBearer's own auto_error
    # would 403 a missing header; we want a uniform 401 plus the
    # WWW-Authenticate challenge the Bearer scheme calls for.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_consumer(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    conn: psycopg.Connection = Depends(get_conn),
) -> Consumer:
    """FastAPI dependency: authenticate the request or raise 401. Every data
    router depends on this, so no data endpoint is reachable without a valid,
    active key. Returns the Consumer (useful for per-request logging later)."""
    if creds is None or not creds.credentials:
        raise _unauthorized()
    consumer = _lookup_consumer(conn, creds.credentials)
    if consumer is None:
        raise _unauthorized()
    return consumer