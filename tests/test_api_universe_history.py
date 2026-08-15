"""Contract tests for GET /v1/universe/{index}/history — the survivorship-free
full membership interval history.

The whole point: this returns EVERY security ever in the index (including long-
delisted names absent from members_asof(today)) and every interval (a security
may have several). These tests lock that survivorship-free property, the reason
the endpoint exists. Auth mirrors test_api_universe.py.
"""
from __future__ import annotations
import os
import secrets
from datetime import date
import psycopg
import pytest
from fastapi.testclient import TestClient
from trading_os.api.app import app
from trading_os.api.deps import hash_key
from trading_os.config import settings

CODE = "SP500"


@pytest.fixture(scope="module")
def api_key():
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


def _auth(k):
    return {"Authorization": f"Bearer {k}"}


def _history(client, api_key):
    r = client.get(f"/v1/universe/{CODE}/history", headers=_auth(api_key))
    assert r.status_code == 200, r.text
    return r.json()


def test_missing_key_is_401(client):
    assert client.get(f"/v1/universe/{CODE}/history").status_code == 401


def test_unknown_index_is_404(client, api_key):
    r = client.get("/v1/universe/NOPE/history", headers=_auth(api_key))
    assert r.status_code == 404


def test_history_is_survivorship_free(client, api_key):
    """The core property: history has MANY more distinct securities than today's
    membership, because it includes delisted/removed names. If these were equal,
    the endpoint would be survivor-only — the bug this endpoint fixes."""
    hist = _history(client, api_key)
    today = client.get(f"/v1/universe/{CODE}", params={"as_of": str(date.today())},
                       headers=_auth(api_key)).json()
    today_ids = {m["security_id"] for m in today["members"]}
    hist_ids = {iv["security_id"] for iv in hist["intervals"]}

    assert hist["security_count"] == len(hist_ids)
    assert hist["interval_count"] == len(hist["intervals"])
    # history strictly supersets today's members
    assert today_ids <= hist_ids
    # and contains securities NOT in today's index (the delisted/removed set)
    delisted = hist_ids - today_ids
    assert len(delisted) > 0, "history has no ex-members — survivorship-free property broken"


def test_history_has_multiple_intervals_for_some_securities(client, api_key):
    """A security can leave and re-enter — so interval_count > security_count."""
    hist = _history(client, api_key)
    assert hist["interval_count"] > hist["security_count"]


def test_intervals_have_identity_and_event_time(client, api_key):
    """Each interval carries security_id (identity) and valid_from; valid_to may be
    null (open). There is NO ticker — identity is the stable id, not a symbol."""
    hist = _history(client, api_key)
    iv = hist["intervals"][0]
    assert "security_id" in iv and "valid_from" in iv
    assert "valid_to" in iv                 # present, may be null
    assert "ticker" not in iv and "symbol" not in iv   # identity is security_id, not a symbol
    # at least one open (current) interval exists
    assert any(x["valid_to"] is None for x in hist["intervals"])


def test_reproducible(client, api_key):
    r1 = client.get(f"/v1/universe/{CODE}/history", headers=_auth(api_key))
    r2 = client.get(f"/v1/universe/{CODE}/history", headers=_auth(api_key))
    assert r1.status_code == r2.status_code == 200
    assert r1.text == r2.text