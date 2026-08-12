"""
Serving-API contract regression tests for GET /v1/features.

Integration tests over the real read stack (auth -> optional resolve_ticker ->
gold lake -> HTTP), driven through Starlette's TestClient against the live
`tradingos` DB with gold computed. If AAPL/gold aren't present, data-dependent
tests skip rather than fail (suite convention).

These freeze the gold endpoint's PUBLIC CONTRACT:
- 401 without a key
- the PIT guarantee: every row's knowledge_time <= as_of
- rows ascending by (implicitly) session_date within a security
- the TRANSPARENCY guarantee: unresolved symbols are reported in `unresolved`,
  never silently dropped
- pinned-as_of reproducibility

Auth setup mirrors test_api_bars.py: a temporary consumer on a short-lived
writable admin connection, revoked on teardown.
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

PINNED_AS_OF = "2026-07-01"
WINDOW = {"start": "2020-01-02", "end": "2020-01-31"}


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


def _auth(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def _require_aapl_features(client, api_key):
    """Fetch the pinned AAPL gold window; skip if gold isn't computed for it."""
    r = client.get(
        "/v1/features",
        params={**WINDOW, "as_of": PINNED_AS_OF, "symbols": ["AAPL"]},
        headers=_auth(api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    if body["count"] == 0:
        pytest.skip("AAPL gold features not present for the pinned window; run the gold refresh")
    return body


def test_missing_key_is_401(client):
    r = client.get("/v1/features")
    assert r.status_code == 401


def test_bad_range_is_422(client, api_key):
    r = client.get(
        "/v1/features",
        params={"start": "2020-02-01", "end": "2020-01-01"},  # start > end
        headers=_auth(api_key),
    )
    assert r.status_code == 422


def test_envelope_shape(client, api_key):
    body = _require_aapl_features(client, api_key)
    assert body["as_of"] == PINNED_AS_OF
    assert body["count"] == len(body["rows"])
    assert body["symbols"] == ["AAPL"]
    assert body["unresolved"] == []          # AAPL resolves
    # full feature row present
    row = body["rows"][0]
    for field in ("session_date", "adj_close", "adj_volume", "knowledge_time",
                  "sma20", "momentum_12_1"):
        assert field in row


def test_pit_knowledge_time_not_after_as_of(client, api_key):
    """Core PIT guarantee for gold: no feature row knowable after the cutoff."""
    body = _require_aapl_features(client, api_key)
    cutoff = datetime(2026, 7, 1, 23, 59, 59, tzinfo=timezone.utc)
    for row in body["rows"]:
        kt = datetime.fromisoformat(row["knowledge_time"])
        assert kt <= cutoff, f"{row['session_date']} knowledge_time {kt} > cutoff"


def test_rows_ascending_by_session_date(client, api_key):
    body = _require_aapl_features(client, api_key)
    dates = [r["session_date"] for r in body["rows"]]
    assert dates == sorted(dates)


def test_unresolved_symbol_is_reported_not_dropped(client, api_key):
    """TRANSPARENCY guarantee: a bad symbol comes back in `unresolved`, and does
    not silently vanish. This is the completeness property the snapshot pull relies on."""
    r = client.get(
        "/v1/features",
        params={**WINDOW, "as_of": PINNED_AS_OF, "symbols": ["AAPL", "NOTATICKER"]},
        headers=_auth(api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "NOTATICKER" in body["unresolved"]        # reported, not dropped
    assert "NOTATICKER" not in (body["symbols"] or [])


def test_all_unresolved_returns_empty_with_report(client, api_key):
    """If nothing resolves, rows are empty but every bad symbol is still reported."""
    r = client.get(
        "/v1/features",
        params={"as_of": PINNED_AS_OF, "symbols": ["NOTATICKER", "ALSOFAKE"]},
        headers=_auth(api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 0
    assert set(body["unresolved"]) == {"NOTATICKER", "ALSOFAKE"}


def test_pinned_as_of_is_reproducible(client, api_key):
    params = {**WINDOW, "as_of": PINNED_AS_OF, "symbols": ["AAPL"]}
    r1 = client.get("/v1/features", params=params, headers=_auth(api_key))
    r2 = client.get("/v1/features", params=params, headers=_auth(api_key))
    assert r1.status_code == r2.status_code == 200
    assert r1.text == r2.text