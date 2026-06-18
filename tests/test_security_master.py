"""
Security master resolution tests.

Two properties:
  1. Real tickers resolve to a stable security_id that carries a real FIGI
     (proves the OpenFIGI enrichment worked and security identity is anchored).
  2. Ticker REUSE resolves correctly per date — the same ticker string mapped
     to TWO different securities over non-overlapping date ranges returns the
     correct security_id for a date in each range. This is the entire reason a
     security master exists; without it, ticker == identity and FIGI is moot.

Test (2) uses a SYNTHETIC fixture inserted inside a transaction that is rolled
back, so it never persists. All other tests are read-only.
"""
from __future__ import annotations

from datetime import date

import pytest

REAL_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "JPM"]


# ---------------------------------------------------------------------------
# 1. Each real ticker resolves to a security with a non-null, real FIGI.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ticker", REAL_TICKERS)
def test_real_ticker_resolves_with_figi(conn, ticker):
    sid = conn.execute(
        "select sec.resolve_ticker(%s, current_date)", (ticker,)
    ).fetchone()
    assert sid and sid[0] is not None, f"{ticker} does not resolve"
    figi = conn.execute(
        "select figi from sec.security where security_id = %s", (sid[0],)
    ).fetchone()[0]
    assert figi is not None and figi.startswith("BBG"), (
        f"{ticker} resolved to security_id {sid[0]} but FIGI is '{figi}' "
        f"(expected a real BBG... FIGI; did OpenFIGI enrichment run?)"
    )


# ---------------------------------------------------------------------------
# 2. security_id is stable: resolving the same ticker twice is identical.
# ---------------------------------------------------------------------------
def test_resolution_is_stable(conn):
    a = conn.execute("select sec.resolve_ticker('AAPL', current_date)").fetchone()[0]
    b = conn.execute("select sec.resolve_ticker('AAPL', current_date)").fetchone()[0]
    assert a == b and a is not None


# ---------------------------------------------------------------------------
# 3. THE reason security masters exist: ticker reuse resolves per date.
#    Synthetic, rolled back — never persisted.
# ---------------------------------------------------------------------------
def test_ticker_reuse_resolves_per_date(conn):
    # Use a savepoint so we can roll back just this synthetic data even though
    # the session is read-only at the default level — we explicitly allow writes
    # within this test by starting a writable subtransaction.
    conn.execute("set transaction read write")
    try:
        with conn.transaction():  # rolled back at the end via the outer rollback
            # Two distinct securities that, in different eras, both listed as 'ZZZX'.
            old_id = conn.execute(
                """
                insert into sec.security (figi, security_type, description, country)
                values ('BBG_TEST_OLD0001', 'EQUITY', 'Old ZZZX Corp (test)', 'US')
                returning security_id
                """
            ).fetchone()[0]
            new_id = conn.execute(
                """
                insert into sec.security (figi, security_type, description, country)
                values ('BBG_TEST_NEW0001', 'EQUITY', 'New ZZZX Inc (test)', 'US')
                returning security_id
                """
            ).fetchone()[0]

            # Old company held ZZZX 1990-2000; new company took it from 2015.
            conn.execute(
                """
                insert into sec.security_identifier
                    (security_id, id_type, id_value, valid_from, valid_to, knowledge_time)
                values (%s, 'TICKER', 'ZZZX', date '1990-01-01', date '2000-12-31', now())
                """,
                (old_id,),
            )
            conn.execute(
                """
                insert into sec.security_identifier
                    (security_id, id_type, id_value, valid_from, valid_to, knowledge_time)
                values (%s, 'TICKER', 'ZZZX', date '2015-01-01', null, now())
                """,
                (new_id,),
            )

            # A 1995 query must hit the OLD company; a 2020 query the NEW one.
            r1995 = conn.execute(
                "select sec.resolve_ticker('ZZZX', date '1995-06-01')"
            ).fetchone()[0]
            r2020 = conn.execute(
                "select sec.resolve_ticker('ZZZX', date '2020-06-01')"
            ).fetchone()[0]

            assert r1995 == old_id, "1995 ZZZX should resolve to the OLD security"
            assert r2020 == new_id, "2020 ZZZX should resolve to the NEW security"

            # A date in the gap (2005) should resolve to neither.
            r2005 = conn.execute(
                "select sec.resolve_ticker('ZZZX', date '2005-06-01')"
            ).fetchone()[0]
            assert r2005 is None, "2005 ZZZX (in the gap) should resolve to nothing"

            # Force rollback of the synthetic data by raising inside the block.
            raise _Rollback()
    except _Rollback:
        pass
    finally:
        conn.rollback()


class _Rollback(Exception):
    """Sentinel to roll back the synthetic fixture without failing the test."""