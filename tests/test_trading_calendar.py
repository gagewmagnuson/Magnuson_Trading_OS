"""
Correctness tests for the trading-calendar connector (ref.trading_session).

These are integration tests against a live PostgreSQL database that has had the
calendar connector run for XNYS. Like the macro/fundamentals tests, the
read-only ones SKIP if XNYS has not been populated yet.

Read-only tests use the shared `conn` fixture (which forces a read-only
transaction). The idempotency test is the one exception: it needs to write, so
it opens its OWN connection, exercises the writer twice inside a transaction it
NEVER commits, and rolls back in a finally — so it can never pollute the DB.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

import psycopg
import pytest


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _xnys_id(conn) -> int | None:
    row = conn.execute(
        "select exchange_id from ref.exchange where mic = 'XNYS'"
    ).fetchone()
    return row[0] if row else None


def _has_sessions(conn, exchange_id: int) -> bool:
    return conn.execute(
        "select 1 from ref.trading_session where exchange_id = %s limit 1",
        (exchange_id,),
    ).fetchone() is not None


def _present(conn, exchange_id: int, d: date) -> bool:
    return conn.execute(
        "select 1 from ref.trading_session "
        "where exchange_id = %s and session_date = %s",
        (exchange_id, d),
    ).fetchone() is not None


@pytest.fixture
def xnys(conn) -> int:
    ex = _xnys_id(conn)
    if ex is None or not _has_sessions(conn, ex):
        pytest.skip("XNYS calendar not populated; run the calendars connector first")
    return ex


# --------------------------------------------------------------------------- #
# 1. A known full-day market holiday is ABSENT, and a normal day is PRESENT.
# --------------------------------------------------------------------------- #
def test_known_market_holiday_closed(conn, xnys):
    # MLK Day 2021-01-18 (Mon) is a full market holiday -> not a session.
    assert not _present(conn, xnys, date(2021, 1, 18)), \
        "MLK Day 2021-01-18 should not be a session"
    # The next trading day (Tue) is a normal, full session -> guards against an
    # empty or mis-populated table silently passing the negative assertion.
    row = conn.execute(
        "select is_half_day from ref.trading_session "
        "where exchange_id = %s and session_date = %s",
        (xnys, date(2021, 1, 19)),
    ).fetchone()
    assert row is not None, "2021-01-19 should be a session"
    assert row[0] is False, "2021-01-19 is a full session, not a half day"


# --------------------------------------------------------------------------- #
# 2. A known early-close session is flagged is_half_day = True.
# --------------------------------------------------------------------------- #
def test_known_half_day(conn, xnys):
    row = conn.execute(
        "select is_half_day from ref.trading_session "
        "where exchange_id = %s and session_date = %s",
        (xnys, date(2024, 7, 3)),
    ).fetchone()
    assert row is not None, "2024-07-03 should be a session"
    assert row[0] is True, "2024-07-03 (day before Independence Day) is an early close"


# --------------------------------------------------------------------------- #
# 3. A full year has a plausible number of sessions (~252).
# --------------------------------------------------------------------------- #
def test_session_count_range(conn, xnys):
    n = conn.execute(
        "select count(*) from ref.trading_session "
        "where exchange_id = %s and session_date >= %s and session_date <= %s",
        (xnys, date(2023, 1, 1), date(2023, 12, 31)),
    ).fetchone()[0]
    assert 248 <= n <= 254, f"2023 session count {n} outside expected 248..254"


# --------------------------------------------------------------------------- #
# 4. Consecutive sessions correctly skip a mid-week holiday.
#    2024-07-03 (Wed, half) -> 2024-07-04 (Thu, CLOSED) -> 2024-07-05 (Fri).
# --------------------------------------------------------------------------- #
def test_consecutive_sessions_skip_holiday(conn, xnys):
    assert _present(conn, xnys, date(2024, 7, 3)), "July 3 should be a session"
    assert not _present(conn, xnys, date(2024, 7, 4)), \
        "July 4 (Independence Day) should be closed"
    assert _present(conn, xnys, date(2024, 7, 5)), "July 5 should be a session"


# --------------------------------------------------------------------------- #
# 5. Idempotency: running the upsert twice yields no duplicate rows.
#    Uses its OWN writable connection and rolls back — never touches real data.
# --------------------------------------------------------------------------- #
def test_idempotency_upsert_no_duplicates():
    from trading_os.connectors.calendars.models import ExchangeMeta, SessionRow
    from trading_os.connectors.calendars.writer import CalendarWriter

    conninfo = os.environ.get("TRADING_OS_PG", "dbname=tradingos")
    sessions = [
        SessionRow(
            date(2024, 7, 3),
            datetime(2024, 7, 3, 13, 30, tzinfo=timezone.utc),
            datetime(2024, 7, 3, 17, 0, tzinfo=timezone.utc),
            True,
        ),
        SessionRow(
            date(2024, 7, 5),
            datetime(2024, 7, 5, 13, 30, tzinfo=timezone.utc),
            datetime(2024, 7, 5, 20, 0, tzinfo=timezone.utc),
            False,
        ),
    ]
    with psycopg.connect(conninfo) as c:
        try:
            w = CalendarWriter(c)
            ex_id = w.ensure_exchange(
                ExchangeMeta("XTST", "Synthetic Test Exchange", "US", "America/New_York")
            )
            w.upsert_sessions(ex_id, sessions)   # first run
            w.upsert_sessions(ex_id, sessions)   # second run (must not duplicate)
            n = c.execute(
                "select count(*) from ref.trading_session where exchange_id = %s",
                (ex_id,),
            ).fetchone()[0]
            assert n == len(sessions), \
                f"expected {len(sessions)} rows after two upserts, got {n}"
        finally:
            c.rollback()  # discard synthetic exchange + sessions; never persist