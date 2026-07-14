"""
Serving-API contract regression tests for GET /v1/macro/{series}.

Integration tests over the real read stack (auth -> observations_asof ->
serialize) via TestClient. Runs against the live `tradingos` DB; skips gracefully
if the FRED connector has not been run.

The centerpiece is test_revisable_series_returns_different_vintages: the same
GDP quarter, queried at two different as_of dates, MUST return different values.
That is the ALFRED lookahead-bias protection (DEC-005) — a backtest asking about
Q1 2020 in mid-2020 must not receive a number that was first published in 2025.

Also frozen here:
  - non-revisable market series have vintage_date == obs_date (DEC-015)
  - vintage_date <= as_of always (the PIT guarantee)
  - series ids are case-insensitive and echoed canonically (DEC-023 reproducibility)
  - 404 unknown series; 200/count=0 for a known series with no data in range
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

# Q1 2020 GDP: released mid-2020, substantially revised since (rebasing +
# annual revisions). The canonical demonstration of why vintages exist.
GDP_SERIES = "GDPC1"
GDP_Q1_2020 = {"start": "2020-01-01", "end": "2020-03-31"}
AS_OF_THEN = "2020-06-01"     # what the world believed in June 2020
AS_OF_NOW = "2026-01-01"      # what we believe now

TREASURY_SERIES = "DGS10"     # non-revisable market series (DEC-015)


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


def _get(client, api_key, series, **params):
    r = client.get(f"/v1/macro/{series}", params=params, headers=_auth(api_key))
    assert r.status_code == 200, r.text
    return r.json()


def test_missing_key_is_401(client):
    assert client.get(f"/v1/macro/{GDP_SERIES}").status_code == 401


def test_unknown_series_is_404(client, api_key):
    r = client.get("/v1/macro/NOTASERIES", headers=_auth(api_key))
    assert r.status_code == 404


def test_start_after_end_is_422(client, api_key):
    r = client.get(
        f"/v1/macro/{GDP_SERIES}",
        params={"start": "2020-06-01", "end": "2020-01-01"},
        headers=_auth(api_key),
    )
    assert r.status_code == 422


def test_revisable_series_returns_different_vintages(client, api_key):
    """THE macro guarantee (DEC-005): the same obs_date, queried at two as_of
    dates, returns the vintage knowable at each. A backtest researching mid-2020
    must NOT see a GDP figure first published years later."""
    then = _get(client, api_key, GDP_SERIES, as_of=AS_OF_THEN, **GDP_Q1_2020)
    now = _get(client, api_key, GDP_SERIES, as_of=AS_OF_NOW, **GDP_Q1_2020)

    if then["count"] == 0 or now["count"] == 0:
        pytest.skip("GDPC1 not ingested; run the FRED connector")

    o_then, o_now = then["observations"][0], now["observations"][0]

    # Same economic period...
    assert o_then["obs_date"] == o_now["obs_date"] == "2020-01-01"
    # ...but different knowledge, and therefore different values.
    assert o_then["value"] != o_now["value"], (
        "Q1-2020 GDP should have been revised between 2020 and today; identical "
        "values mean vintage selection is broken (serving latest-known regardless "
        "of as_of) — a lookahead-bias hole."
    )
    assert o_then["vintage_date"] < o_now["vintage_date"]


def test_vintage_never_after_as_of(client, api_key):
    """The PIT guarantee: no returned row was published after the knowledge cutoff."""
    body = _get(client, api_key, GDP_SERIES, as_of=AS_OF_THEN, **GDP_Q1_2020)
    if body["count"] == 0:
        pytest.skip("GDPC1 not ingested")
    cutoff = date.fromisoformat(AS_OF_THEN)
    for o in body["observations"]:
        assert date.fromisoformat(o["vintage_date"]) <= cutoff


def test_non_revisable_series_has_vintage_equal_obs_date(client, api_key):
    """DEC-015: market-observed series are single-vintage — the value was known on
    the observation date and is never restated."""
    body = _get(
        client, api_key, TREASURY_SERIES,
        as_of="2026-01-01", start="2025-12-01", end="2025-12-10",
    )
    if body["count"] == 0:
        pytest.skip("DGS10 not ingested")
    for o in body["observations"]:
        assert o["vintage_date"] == o["obs_date"]


def test_observations_ascend_by_obs_date(client, api_key):
    body = _get(client, api_key, GDP_SERIES, as_of=AS_OF_NOW, start="2015-01-01")
    if body["count"] == 0:
        pytest.skip("GDPC1 not ingested")
    dates = [o["obs_date"] for o in body["observations"]]
    assert dates == sorted(dates)


def test_series_id_is_case_insensitive_and_echoed_canonically(client, api_key):
    """Lowercase in, canonical out — so two differently-cased requests are
    byte-identical (DEC-023 reproducibility)."""
    lower = client.get(
        f"/v1/macro/{GDP_SERIES.lower()}",
        params={"as_of": AS_OF_NOW, **GDP_Q1_2020},
        headers=_auth(api_key),
    )
    upper = client.get(
        f"/v1/macro/{GDP_SERIES}",
        params={"as_of": AS_OF_NOW, **GDP_Q1_2020},
        headers=_auth(api_key),
    )
    assert lower.status_code == upper.status_code == 200
    assert lower.json()["series_id"] == GDP_SERIES
    assert lower.text == upper.text


def test_pinned_as_of_is_reproducible(client, api_key):
    params = {"as_of": AS_OF_NOW, **GDP_Q1_2020}
    r1 = client.get(f"/v1/macro/{GDP_SERIES}", params=params, headers=_auth(api_key))
    r2 = client.get(f"/v1/macro/{GDP_SERIES}", params=params, headers=_auth(api_key))
    assert r1.status_code == r2.status_code == 200
    assert r1.text == r2.text


def test_known_series_empty_range_is_200_not_error(client, api_key):
    """A known series with no observations in range: 200 with count=0, not 404."""
    r = client.get(
        f"/v1/macro/{GDP_SERIES}",
        params={"as_of": AS_OF_NOW, "start": "1900-01-01", "end": "1900-12-31"},
        headers=_auth(api_key),
    )
    assert r.status_code == 200
    assert r.json()["count"] == 0