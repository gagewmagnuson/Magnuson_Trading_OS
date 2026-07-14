"""
Serving-API contract regression tests for GET /v1/fundamentals/{symbol}.

Integration tests over the real read stack (auth -> resolve_ticker ->
fundamentals_asof -> serialize), driven through TestClient. Runs against the live
`tradingos` DB; skips gracefully if AAPL fundamentals are absent.

Freezes the endpoint's contract and, critically, DEC-014's duration semantics:
  - annual flows fall in the 350..380 band; quarterly in 85..95
  - annual and quarterly NEVER mix in one response
  - instants (period_start IS NULL) ride along with flows to form a snapshot
  - `fiscal_period` is NOT trusted for duration (real data mislabels quarters 'FY')
  - value keeps exact precision (JSON string, not float)
  - PIT: knowledge_time <= as_of; pinned as_of is reproducible
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from trading_os.api.app import app
from trading_os.api.deps import hash_key
from trading_os.config import settings

# Same pinned knowledge date test_pit_fundamentals.py uses for its verified anchors.
PINNED_AS_OF = "2018-01-01"

# DEC-014 bands (frozen).
ANNUAL_MIN, ANNUAL_MAX = 350, 380
QUARTERLY_MIN, QUARTERLY_MAX = 85, 95

# Ground truth: Apple FY2017 revenue, as known on 2018-01-01.
AAPL_FY2017_REVENUE = "229234000000"


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


def _get(client, api_key, **params):
    params.setdefault("as_of", PINNED_AS_OF)
    r = client.get("/v1/fundamentals/AAPL", params=params, headers=_auth(api_key))
    assert r.status_code == 200, r.text
    body = r.json()
    if body["count"] == 0 and "statement" not in params:
        pytest.skip("AAPL fundamentals not ingested; run the EDGAR connector")
    return body


def test_missing_key_is_401(client):
    assert client.get("/v1/fundamentals/AAPL").status_code == 401


def test_unresolved_ticker_is_404(client, api_key):
    r = client.get("/v1/fundamentals/NOTATICKER", headers=_auth(api_key))
    assert r.status_code == 404


def test_bad_period_type_is_422(client, api_key):
    r = client.get(
        "/v1/fundamentals/AAPL", params={"period_type": "monthly"}, headers=_auth(api_key)
    )
    assert r.status_code == 422


def test_default_period_type_is_annual_and_echoed(client, api_key):
    body = _get(client, api_key)
    assert body["period_type"] == "annual"
    assert body["count"] == len(body["facts"])


def test_annual_flows_are_in_dec014_band(client, api_key):
    """Annual flows fall in 350..380 days. Apple's 52/53-week year yields 363/370 —
    which is exactly why DEC-014 mandates a BAND, not an equality on 365."""
    body = _get(client, api_key, concept="revenue")
    flows = [f for f in body["facts"] if f["period_start"] is not None]
    assert flows, "expected annual revenue flows"
    for f in flows:
        assert ANNUAL_MIN <= f["duration_days"] <= ANNUAL_MAX


def test_quarterly_flows_are_in_dec014_band(client, api_key):
    body = _get(client, api_key, concept="revenue", period_type="quarterly")
    flows = [f for f in body["facts"] if f["period_start"] is not None]
    assert flows, "expected quarterly revenue flows"
    for f in flows:
        assert QUARTERLY_MIN <= f["duration_days"] <= QUARTERLY_MAX


def test_annual_and_quarterly_never_mix(client, api_key):
    """The core DEC-014 guarantee: one response never contains two flow durations."""
    annual = _get(client, api_key, concept="revenue")
    quarterly = _get(client, api_key, concept="revenue", period_type="quarterly")
    a_durations = {f["duration_days"] for f in annual["facts"] if f["period_start"]}
    q_durations = {f["duration_days"] for f in quarterly["facts"] if f["period_start"]}
    assert a_durations and q_durations
    assert not (a_durations & q_durations), "annual and quarterly durations overlapped"


def test_fiscal_period_label_is_not_trusted_for_duration(client, api_key):
    """Real EDGAR data labels 90-day quarters as 'FY'. DEC-014 requires duration to
    come from dates, never the label. This asserts the mislabeling exists AND that
    the band filter still returned only true quarters."""
    body = _get(client, api_key, concept="revenue", period_type="quarterly")
    flows = [f for f in body["facts"] if f["period_start"] is not None]
    assert any(f["fiscal_period"] == "FY" for f in flows), (
        "expected the known EDGAR mislabeling (90-day quarters tagged 'FY')"
    )
    for f in flows:
        assert QUARTERLY_MIN <= f["duration_days"] <= QUARTERLY_MAX


def test_instants_have_no_duration(client, api_key):
    body = _get(client, api_key, period_type="instant")
    assert body["facts"], "expected instant (balance-sheet) facts"
    for f in body["facts"]:
        assert f["period_start"] is None
        assert f["duration_days"] is None


def test_snapshot_includes_instants_alongside_flows(client, api_key):
    """period_type=annual returns a COMPLETE snapshot: annual flows + balance sheet."""
    body = _get(client, api_key)
    has_flow = any(f["period_start"] is not None for f in body["facts"])
    has_instant = any(f["period_start"] is None for f in body["facts"])
    assert has_flow and has_instant, "annual snapshot must include flows AND instants"


def test_value_precision_is_exact(client, api_key):
    """Values serialize as exact strings (numeric, never float). FY2017 revenue is
    ground truth verified by test_pit_fundamentals.py."""
    body = _get(client, api_key, concept="revenue")
    fy2017 = [f for f in body["facts"] if f["period_end_date"] == "2017-09-30"]
    assert fy2017, "expected FY2017 revenue"
    assert fy2017[0]["value"] == AAPL_FY2017_REVENUE


def test_pit_knowledge_time_not_after_as_of(client, api_key):
    body = _get(client, api_key)
    cutoff = datetime(2018, 1, 1, 23, 59, 59, tzinfo=timezone.utc)
    for f in body["facts"]:
        assert datetime.fromisoformat(f["knowledge_time"]) <= cutoff


def test_pinned_as_of_is_reproducible(client, api_key):
    params = {"as_of": PINNED_AS_OF, "concept": "revenue"}
    r1 = client.get("/v1/fundamentals/AAPL", params=params, headers=_auth(api_key))
    r2 = client.get("/v1/fundamentals/AAPL", params=params, headers=_auth(api_key))
    assert r1.status_code == r2.status_code == 200
    assert r1.text == r2.text


def test_impossible_filter_combo_is_empty_not_error(client, api_key):
    r = client.get(
        "/v1/fundamentals/AAPL",
        params={"as_of": PINNED_AS_OF, "concept": "revenue", "statement": "balance"},
        headers=_auth(api_key),
    )
    assert r.status_code == 200
    assert r.json()["count"] == 0