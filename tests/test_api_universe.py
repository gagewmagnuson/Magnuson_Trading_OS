"""Serving-API contract tests for GET /v1/universe/{index}.

Freezes the public contract: 401 without a key, 404 for an unknown index,
event-time PIT membership (DEC-027) incl. the survivorship property, and
pinned-as_of reproducibility. Auth mirrors test_api_bars.py.
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
D2008 = "2008-06-30"


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


def _require_members(client, api_key):
    r = client.get(f"/v1/universe/{CODE}", params={"as_of": D2008}, headers=_auth(api_key))
    assert r.status_code == 200, r.text
    body = r.json()
    if body["count"] == 0:
        pytest.skip("SP500 membership not loaded; run the universe expansion")
    return body


def test_missing_key_is_401(client):
    r = client.get(f"/v1/universe/{CODE}")
    assert r.status_code == 401


def test_unknown_index_is_404(client, api_key):
    r = client.get("/v1/universe/NOT_A_REAL_INDEX", headers=_auth(api_key))
    assert r.status_code == 404


def test_envelope_and_membership(client, api_key):
    body = _require_members(client, api_key)
    assert body["index"] == CODE
    assert body["as_of"] == D2008
    assert body["count"] == len(body["members"])
    assert body["count"] > 100
    # members ascending by security_id; each has a security_id
    ids = [m["security_id"] for m in body["members"]]
    assert ids == sorted(ids)
    # at least some members resolved a PIT ticker
    assert any(m["symbol"] for m in body["members"])


def test_membership_is_pit_not_today(client, api_key):
    """Survivorship: the 2008 membership must differ from today's (delisted names
    present in 2008 that are gone now)."""
    b2008 = _require_members(client, api_key)
    today = client.get(f"/v1/universe/{CODE}", params={"as_of": str(date.today())},
                       headers=_auth(api_key)).json()
    ids2008 = {m["security_id"] for m in b2008["members"]}
    ids_today = {m["security_id"] for m in today["members"]}
    assert ids2008 - ids_today, "no survivorship drops via the API — PIT rule broken"


def test_pinned_as_of_reproducible(client, api_key):
    p = {"as_of": D2008}
    r1 = client.get(f"/v1/universe/{CODE}", params=p, headers=_auth(api_key))
    r2 = client.get(f"/v1/universe/{CODE}", params=p, headers=_auth(api_key))
    assert r1.status_code == r2.status_code == 200
    assert r1.text == r2.text