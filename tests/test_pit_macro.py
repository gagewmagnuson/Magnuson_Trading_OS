"""
Point-in-time correctness regression tests for macro (FRED/ALFRED) data.

The macro analog of the EDGAR lookahead guard: a revised series must return its
ORIGINALLY PUBLISHED value when queried as of an early date, and the REVISED
value when queried later. GDP (GDPC1) is the canonical revised series.

These tests assert STRUCTURAL vintage correctness without hard-coding exact
GDP values (which depend on which vintages your ingest captured). They verify
the bitemporal MECHANISM works, which is what we need to protect against
regression. A value-specific test can be added once you inspect your data.

Requires the FRED connector to have ingested at least GDPC1 and UNRATE.
"""
from __future__ import annotations

import pytest

REVISED_SERIES = "GDPC1"   # heavily revised -> multiple vintages expected


def _has_series(conn, series_id: str) -> bool:
    row = conn.execute(
        "select 1 from macro.observation where series_id = %s limit 1", (series_id,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# 1. The revised series actually has multiple vintages for some obs_date.
#    If this fails, ALFRED vintages weren't captured (plain FRED was used).
# ---------------------------------------------------------------------------
def test_revised_series_has_multiple_vintages(conn):
    if not _has_series(conn, REVISED_SERIES):
        pytest.skip(f"{REVISED_SERIES} not ingested")
    row = conn.execute(
        """
        select obs_date, count(*) as n
        from macro.observation
        where series_id = %s
        group by obs_date
        having count(*) > 1
        order by n desc
        limit 1
        """,
        (REVISED_SERIES,),
    ).fetchone()
    assert row is not None, (
        f"{REVISED_SERIES} has no obs_date with >1 vintage. ALFRED vintage "
        f"capture is broken (looks like latest-only FRED data)."
    )


# ---------------------------------------------------------------------------
# 2. As-of monotonicity: for an obs_date with multiple vintages, the value
#    known as of the EARLIEST vintage differs from the value known as of the
#    LATEST vintage (a revision actually changed the number), and as_of
#    correctly returns the vintage in effect at the query date.
# ---------------------------------------------------------------------------
def test_asof_returns_vintage_in_effect(conn):
    if not _has_series(conn, REVISED_SERIES):
        pytest.skip(f"{REVISED_SERIES} not ingested")

    # Find an obs_date whose value was actually revised (>=2 distinct values).
    target = conn.execute(
        """
        select obs_date
        from macro.observation
        where series_id = %s and value is not null
        group by obs_date
        having count(distinct value) >= 2
        order by obs_date
        limit 1
        """,
        (REVISED_SERIES,),
    ).fetchone()
    if target is None:
        pytest.skip(f"No revised obs_date found for {REVISED_SERIES}")
    obs_date = target[0]

    # The two extreme vintages for that obs_date.
    vintages = conn.execute(
        """
        select vintage_date, value
        from macro.observation
        where series_id = %s and obs_date = %s and value is not null
        order by vintage_date
        """,
        (REVISED_SERIES, obs_date),
    ).fetchall()
    earliest_vintage, earliest_value = vintages[0]
    latest_vintage, latest_value = vintages[-1]
    assert earliest_value != latest_value, "expected a real revision"

    # as_of the earliest vintage date -> earliest value.
    got_early = conn.execute(
        """
        select value from macro.observations_asof(%s)
        where series_id = %s and obs_date = %s
        """,
        (earliest_vintage, REVISED_SERIES, obs_date),
    ).fetchone()
    assert got_early is not None and got_early[0] == earliest_value, (
        f"as_of({earliest_vintage}) should return the originally published "
        f"value {earliest_value}, got {got_early}"
    )

    # as_of the latest vintage date -> latest value.
    got_late = conn.execute(
        """
        select value from macro.observations_asof(%s)
        where series_id = %s and obs_date = %s
        """,
        (latest_vintage, REVISED_SERIES, obs_date),
    ).fetchone()
    assert got_late is not None and got_late[0] == latest_value, (
        f"as_of({latest_vintage}) should return the revised value "
        f"{latest_value}, got {got_late}"
    )


# ---------------------------------------------------------------------------
# 3. No future leakage: as_of a date returns no vintage published after it.
# ---------------------------------------------------------------------------
def test_no_future_vintage_leakage(conn):
    if not _has_series(conn, REVISED_SERIES):
        pytest.skip(f"{REVISED_SERIES} not ingested")
    # Pick a mid-history vintage date and assert nothing newer leaks in.
    mid = conn.execute(
        """
        select vintage_date from macro.observation
        where series_id = %s order by vintage_date
        offset (select count(distinct vintage_date)/2 from macro.observation where series_id=%s)
        limit 1
        """,
        (REVISED_SERIES, REVISED_SERIES),
    ).fetchone()[0]

    leaked = conn.execute(
        """
        select count(*) from macro.observations_asof(%s) f
        join (select obs_date, vintage_date from macro.observation
              where series_id=%s) v
          on v.obs_date = f.obs_date
        where f.series_id = %s and v.vintage_date > %s and f.value = (
              select value from macro.observation o2
              where o2.series_id=%s and o2.obs_date=f.obs_date
                and o2.vintage_date=v.vintage_date)
        """,
        (mid, REVISED_SERIES, REVISED_SERIES, mid, REVISED_SERIES),
    ).fetchone()[0]
    assert leaked == 0, (
        f"as_of({mid}) returned a value from a vintage published after {mid}: "
        f"macro lookahead bias."
    )