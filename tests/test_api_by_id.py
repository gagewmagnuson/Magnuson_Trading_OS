"""by-id retrieval tests — the survivorship-critical invariant.

A snapshot must retrieve EVERY historical member regardless of whether its
historical ticker is still valid at the knowledge cutoff. The by-id routes make
that possible; the symbol routes correctly 404 for a delisted ticker at a current
as_of. AABA (security 544, ticker valid 1996-04-12..2019-11-06) is the regression
case: delisted years ago, ticker long invalid, but its data must remain reachable
by security_id.
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

AABA_SECURITY_ID = 544
AABA_TICKER = "AABA"
CURRENT_AS_OF = "2026-08-15"     # long after AABA delisted (2019-11-06)


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


def test_symbol_route_404s_for_delisted_ticker_at_current_asof(client, api_key):
    """The exact failure the by-id route fixes: AABA's ticker does not resolve at
    a current knowledge cutoff, so the symbol route correctly 404s."""
    r = client.get(f"/v1/bars/{AABA_TICKER}", params={"as_of": CURRENT_AS_OF},
                   headers=_auth(api_key))
    assert r.status_code == 404


def test_bars_by_id_retrieves_delisted_security(client, api_key):
    """by-id retrieves AABA's full history at the SAME current cutoff where the
    symbol route fails — the survivorship-critical guarantee."""
    r = client.get(f"/v1/bars/by-id/{AABA_SECURITY_ID}",
                   params={"as_of": CURRENT_AS_OF}, headers=_auth(api_key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["security_id"] == AABA_SECURITY_ID
    assert body["count"] > 0                       # real bars returned
    sessions = [b["session_date"] for b in body["bars"]]
    assert min(sessions) < "2000-01-01"            # history reaches back to the 1990s
    assert max(sessions) <= "2019-11-06"           # and ends at/ before delisting


def test_features_by_id_retrieves_delisted_security(client, api_key):
    """Gold features by-id likewise reachable for the delisted security."""
    r = client.get(f"/v1/features/by-id/{AABA_SECURITY_ID}",
                   params={"as_of": CURRENT_AS_OF}, headers=_auth(api_key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] > 0
    sessions = [row["session_date"] for row in body["rows"]]
    assert max(sessions) <= "2019-11-06"


def test_by_id_unknown_security_404s(client, api_key):
    r = client.get("/v1/bars/by-id/99999999", params={"as_of": CURRENT_AS_OF},
                   headers=_auth(api_key))
    assert r.status_code == 404


def test_by_id_missing_key_401(client):
    assert client.get(f"/v1/bars/by-id/{AABA_SECURITY_ID}").status_code == 401