"""
Shared pytest fixtures for Trading OS tests.

These tests run against a live PostgreSQL database that already has the schema
applied and the EDGAR validation cohort ingested. They are integration tests,
not unit tests: their job is to guarantee point-in-time correctness end to end,
which is the property the whole system exists to provide.

Connection: set TRADING_OS_PG (libpq conninfo) or rely on the default
'dbname=tradingos'. Tests are READ-ONLY; they never modify data.
"""
from __future__ import annotations

import os

import psycopg
import pytest


@pytest.fixture(scope="session")
def conn():
    conninfo = os.environ.get("TRADING_OS_PG", "dbname=tradingos")
    with psycopg.connect(conninfo) as c:
        # Hard guarantee: these tests must never write.
        c.execute("set default_transaction_read_only = on")
        yield c


def _security_id(conn, ticker: str) -> int | None:
    row = conn.execute(
        "select sec.resolve_ticker(%s, current_date)", (ticker,)
    ).fetchone()
    return row[0] if row else None


@pytest.fixture
def aapl_id(conn) -> int:
    sid = _security_id(conn, "AAPL")
    if sid is None:
        pytest.skip("AAPL not ingested; run the EDGAR connector first")
    return sid