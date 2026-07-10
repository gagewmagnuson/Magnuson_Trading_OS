"""
Serving-API contract regression tests for GET /v1/bars/{symbol}.

Integration tests over the real read stack (auth -> resolve_ticker -> lake ->
adjustment -> HTTP), driven through Starlette's TestClient. They run against the
live `tradingos` DB with bars ingested; if AAPL or its bars are absent they skip
rather than fail, matching the suite's convention (conftest.py).

These freeze the bars endpoint's PUBLIC CONTRACT so a later refactor cannot
silently break it:
  - 401 without a key; 404 for an unresolved ticker; 422 for a bad adjustment
  - bars ascending by session_date
  - the PIT guarantee: every row's knowledge_time <= as_of
  - pinned-as_of reproducibility: identical pinned requests -> identical bytes

Auth setup mints a temporary consumer via a short-lived WRITABLE admin
connection (as keys.py does) and revokes it in teardown; the shared `conn`
fixture stays read-only.
"""
from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from trading_os.api.app import app
from trading_os.api.deps import hash_key
from trading_os.config import settings

# A pinned knowledge date for reproducible assertions. Well after the bars were
# ingested, so the full history is knowable; fixed so the PIT results never move.
PINNED_AS_OF = "2026-07-01"
WINDOW = {"start": "2016-01-04", "end": "2016-01-08"}  # a known 5-session week


@pytest.fixture(scope="module")
def api_key():
    """Mint a temporary API consumer, yield its raw key, revoke on teardown.

    Uses its own writable connection (never the read-only `conn` fixture), the
    same admin-writer boundary keys.py enforces. Soft-deletes on teardown.
    """
    label = f"pytest-{secrets.token_hex(4)}"
    raw_key = "tos_" + secrets.token_urlsafe(32)
    conninfo = os.environ.get("TRADING_OS_PG", settings.pg_conninfo())
    admin = psycopg.connect(conninfo)
    admin.autocommit = True
    admin.execute(
        "INSERT INTO meta.api_consumer (label, key_hash, key_prefix) VALUES (%s, %s, %s)",
        [label, hash_key(raw_key), raw_key[:12]],
    )
    try:
        yield raw_key
    finally:
        admin.execute("DELETE FROM meta.api_consumer WHERE label = %s", [label])
        admin.close()


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _auth(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def _require_aapl(client, api_key):
    """Fetch the pinned AAPL window; skip if AAPL/bars aren't ingested."""
    r = client.get(
        "/v1/bars/AAPL",
        params={**WINDOW, "as_of": PINNED_AS_OF},
        headers=_auth(api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    if body["count"] == 0:
        pytest.skip("AAPL bars not ingested for the pinned window; run the Alpaca connector")
    return body


def test_missing_key_is_401(client):
    r = client.get("/v1/bars/AAPL")
    assert r.status_code == 401


def test_bad_key_is_401(client):
    r = client.get("/v1/bars/AAPL", headers={"Authorization": "Bearer tos_not_a_real_key"})
    assert r.status_code == 401


def test_unresolved_ticker_is_404(client, api_key):
    r = client.get("/v1/bars/NOTATICKER", headers=_auth(api_key))
    assert r.status_code == 404


def test_bad_adjustment_is_422(client, api_key):
    r = client.get("/v1/bars/AAPL", params={"adjustment": "banana"}, headers=_auth(api_key))
    assert r.status_code == 422


def test_envelope_shape(client, api_key):
    body = _require_aapl(client, api_key)
    assert body["symbol"] == "AAPL"
    assert isinstance(body["security_id"], int)
    assert body["as_of"] == PINNED_AS_OF
    assert body["adjustment"] == "none"
    assert body["count"] == len(body["bars"])


def test_bars_ascending_by_session_date(client, api_key):
    body = _require_aapl(client, api_key)
    dates = [b["session_date"] for b in body["bars"]]
    assert dates == sorted(dates)


def test_pit_knowledge_time_not_after_as_of(client, api_key):
    """The core PIT guarantee: no row is knowable after the as_of cutoff."""
    body = _require_aapl(client, api_key)
    cutoff = datetime(2026, 7, 1, 23, 59, 59, tzinfo=timezone.utc)  # PINNED_AS_OF, end-of-day UTC
    for b in body["bars"]:
        kt = datetime.fromisoformat(b["knowledge_time"])
        assert kt <= cutoff, f"{b['session_date']} knowledge_time {kt} > as_of cutoff"


def test_pinned_as_of_is_reproducible(client, api_key):
    """Identical pinned-as_of requests must return byte-identical responses."""
    url = "/v1/bars/AAPL"
    params = {**WINDOW, "as_of": PINNED_AS_OF}
    r1 = client.get(url, params=params, headers=_auth(api_key))
    r2 = client.get(url, params=params, headers=_auth(api_key))
    assert r1.status_code == r2.status_code == 200
    assert r1.text == r2.text


def test_total_return_adjustment_runs(client, api_key):
    """The adjusted path (conditional Postgres attach) returns 200 and stays ordered."""
    r = client.get(
        "/v1/bars/AAPL",
        params={"adjustment": "total_return", "start": "2020-01-01", "end": "2020-12-31",
                "as_of": PINNED_AS_OF},
        headers=_auth(api_key),
    )
    assert r.status_code == 200, r.text
    dates = [b["session_date"] for b in r.json()["bars"]]
    assert dates == sorted(dates)