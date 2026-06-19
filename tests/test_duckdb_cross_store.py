"""
Cross-store point-in-time capstone tests (V0 gate).

Prove that the Parquet -> DuckDB -> attached-Postgres path PRESERVES
point-in-time semantics — the core architectural claim of the Trading OS.
Postgres-only PIT is already proven (test_pit_macro / test_pit_fundamentals);
here we prove the composition across the store boundary.

Requires macro.observation populated (FRED connector) and a working DuckDB
postgres attach. Skips cleanly if either is unavailable. The Parquet snapshot is
exported into a pytest temp directory, so these tests never touch the real
lake/ directory.
"""
from __future__ import annotations

import pytest

from trading_os.engine.config import (DEFAULT_AS_OF, VINTAGE_DEMO_SERIES,
                                       EngineConfig)


def _approx_eq(a, b, rel: float = 1e-9, abs_: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is b
    fa, fb = float(a), float(b)
    return abs(fa - fb) <= max(abs_, rel * max(abs(fa), abs(fb)))


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    from trading_os.engine.store import DuckDBStore
    cfg = EngineConfig(lake_root=tmp_path_factory.mktemp("lake"))
    st = DuckDBStore(cfg)
    try:
        st.connect()
    except Exception as e:  # extension/attach unavailable in this environment
        pytest.skip(f"DuckDB postgres attach unavailable: {e}")
    n = st.export_macro_observations()
    if n == 0:
        st.close()
        pytest.skip("macro.observation is empty; run the FRED connector first")
    yield st
    st.close()


def _vintages(conn, series_id) -> dict:
    """obs_date -> [(vintage_date, value), ...] sorted ascending by vintage_date."""
    rows = conn.execute(
        "select obs_date, vintage_date, value from macro.observation "
        "where series_id = %s and value is not null order by obs_date, vintage_date",
        (series_id,),
    ).fetchall()
    by_date: dict = {}
    for obs_date, vintage, value in rows:
        by_date.setdefault(obs_date, []).append((vintage, value))
    return by_date


def _first_vs_last(by_date):
    """An obs_date whose EARLIEST and LATEST vintage values differ.
    Returns (obs_date, (v1, val1), (v2, val2)) or None."""
    for obs_date, vs in by_date.items():
        if len(vs) >= 2 and not _approx_eq(vs[0][1], vs[-1][1]):
            return obs_date, vs[0], vs[-1]
    return None


def _last_revision(by_date):
    """An obs_date whose LAST revision changed the value: the final two
    CONSECUTIVE vintages differ. Returns (obs_date, (v_prev, val_prev),
    (v_last, val_last)) or None. This is what a no-lookahead test needs —
    a series like GDPC1 has many intermediate vintages, so the value in effect
    just before the last revision is the PRIOR vintage's value, not the first."""
    for obs_date, vs in by_date.items():
        if len(vs) >= 2 and not _approx_eq(vs[-2][1], vs[-1][1]):
            return obs_date, vs[-2], vs[-1]
    return None


# 1. A revision changes the as-known value, seen THROUGH the Parquet/DuckDB path.
def test_vintage_changes_across_revision(conn, store):
    rec = _first_vs_last(_vintages(conn, VINTAGE_DEMO_SERIES))
    if rec is None:
        pytest.skip(f"{VINTAGE_DEMO_SERIES} has no obs_date with differing first/last vintages")
    obs_date, (v1, val1), (v2, val2) = rec
    early = store.macro_value_asof(VINTAGE_DEMO_SERIES, obs_date, v1)
    late = store.macro_value_asof(VINTAGE_DEMO_SERIES, obs_date, v2)
    assert early is not None and late is not None
    assert _approx_eq(early, val1), "as-of the first vintage, expected the original value"
    assert _approx_eq(late, val2), "as-of the latest vintage, expected the revised value"
    assert not _approx_eq(early, late)


# 2. No lookahead across the boundary: a revision is invisible BEFORE its vintage_date.
#    Uses CONSECUTIVE vintages (prior vs latest): the value in effect just before
#    the last revision is the PRIOR vintage's value — GDPC1 has many intermediate
#    revisions, so it is NOT the earliest value. The revised value must appear
#    only AT its vintage_date, never earlier.
def test_no_lookahead_across_boundary(conn, store):
    rec = _last_revision(_vintages(conn, VINTAGE_DEMO_SERIES))
    if rec is None:
        pytest.skip(f"{VINTAGE_DEMO_SERIES} has no obs_date whose last revision changed the value")
    obs_date, (v_prev, val_prev), (v_last, val_last) = rec
    asof_prev = store.macro_value_asof(VINTAGE_DEMO_SERIES, obs_date, v_prev)
    asof_last = store.macro_value_asof(VINTAGE_DEMO_SERIES, obs_date, v_last)
    assert _approx_eq(asof_prev, val_prev), "as-of the prior vintage, expected the prior value"
    assert not _approx_eq(asof_prev, val_last), \
        "the revised value must NOT be visible before its vintage_date"
    assert _approx_eq(asof_last, val_last), \
        "as-of the latest vintage, the revised value must be visible"


# 3. Equivalence: the cross-store path agrees with native Postgres macro.observations_asof.
def test_parquet_path_equals_postgres_asof(conn, store):
    as_of = DEFAULT_AS_OF
    pg_rows = conn.execute(
        "select series_id, obs_date, value from macro.observations_asof(%s) "
        "where series_id = %s",
        (as_of, VINTAGE_DEMO_SERIES),
    ).fetchall()
    if not pg_rows:
        pytest.skip(f"no {VINTAGE_DEMO_SERIES} data as of {as_of}")
    checked = mismatches = 0
    for series_id, obs_date, pg_val in pg_rows:
        duck_val = store.macro_value_asof(series_id, obs_date, as_of)
        checked += 1
        if not _approx_eq(duck_val, pg_val):
            mismatches += 1
    assert checked > 0
    assert mismatches == 0, (
        f"{mismatches}/{checked} (series,obs_date) disagreed between the "
        f"Parquet/DuckDB path and Postgres macro.observations_asof — the store "
        f"boundary is NOT preserving point-in-time."
    )


# 4. The capstone query executes and returns a coherent as-of world state.
def test_capstone_world_state_runs(store):
    rows = store.capstone_world_state(DEFAULT_AS_OF)
    assert rows, "capstone returned no rows (expected one per security with total_assets)"
    for security_id, figi, description, total_assets, period_end_date, ctx_yield in rows:
        assert security_id is not None
        assert total_assets is not None
        assert ctx_yield is not None, "macro context (10Y yield) missing from the cross-store join"