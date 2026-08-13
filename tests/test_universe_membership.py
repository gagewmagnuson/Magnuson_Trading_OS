"""Tests for univ.members_asof — the PIT universe membership read (DEC-027).

Event-time semantics: membership is 'who was in the index on the as_of date' by
valid_from/valid_to containment, NOT knowledge_time gated (DEC-027 / migration
008). The survivorship property — delisted/removed members still appear for
historical dates — is the whole point, and the reason naive 'today's constituents'
backtests are wrong.

Runs against the live tradingos DB; skips if SP500 membership isn't loaded.
"""
from __future__ import annotations
from datetime import date
import psycopg
import pytest
from trading_os.config import settings

CODE = "SP500"
D2008 = date(2008, 6, 30)
D2020 = date(2020, 1, 2)


@pytest.fixture(scope="module")
def conn():
    c = psycopg.connect(settings.pg_conninfo())
    c.autocommit = True
    try:
        yield c
    finally:
        c.close()


def _members(conn, code, as_of):
    return {r[0] for r in conn.execute(
        "select security_id from univ.members_asof(%s, %s)", [code, as_of]
    ).fetchall()}


def _require(conn):
    m = _members(conn, CODE, D2008)
    if not m:
        pytest.skip("SP500 membership not loaded; run the universe expansion")
    return m


def test_membership_nonempty_historical(conn):
    m = _require(conn)
    assert len(m) > 100   # S&P 500 ~ hundreds; sanity, not an exact band


def test_membership_differs_by_date(conn):
    _require(conn)
    m2008 = _members(conn, CODE, D2008)
    m2020 = _members(conn, CODE, D2020)
    # Membership is time-varying: the two dates must not be identical sets.
    assert m2008 != m2020


def test_survivorship_delisted_members_still_returned(conn):
    """THE core PIT property: securities in the 2008 index that are NOT in
    today's index still appear for the 2008 date. A naive 'today's constituents'
    read would wrongly omit these — the exact bias the event-time rule prevents."""
    _require(conn)
    m2008 = _members(conn, CODE, D2008)
    today = _members(conn, CODE, date.today())
    dropped = m2008 - today
    assert len(dropped) > 0, "no survivorship drops — event-time rule may be broken"


def test_nonexistent_code_returns_empty(conn):
    # members_asof for a code that doesn't exist yields no rows (the ROUTER turns
    # this into 404; the function itself just returns empty).
    assert _members(conn, "NOT_A_REAL_INDEX", D2008) == set()