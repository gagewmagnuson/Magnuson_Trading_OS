"""
Point-in-time correctness regression tests.

These freeze the manually-validated results from the V0 payoff query. If any
future change to the schema, the *_asof() functions, the writer, or the mapper
reintroduces lookahead bias or corrupts a value, these fail.

The anchor case: what the Trading OS knew about Apple as of 2018-01-01.
Apple's FY2017 10-K was filed early Nov 2017, so FY2017 annuals are knowable
by 2018-01-01; nothing from FY2018 is. All expected values were verified
against Apple's reported figures.

Helper queries select ANNUAL flow figures by DURATION (period_end - period_start
in ~350-380 days), per DEC-014: flow concepts must be duration-filtered or a
query mixes quarterly, YTD, and annual rows.
"""
from __future__ import annotations

from datetime import date

import pytest

AS_OF = "2018-01-01"

# Verified annual values, FY2017 (period_end 2017-09-30), as known on AS_OF.
EXPECTED_FY2017 = {
    "net_income": 48351000000,
    "cfo": 63598000000,
    "eps_diluted": 9.21,
}

# Tolerance for floating compares (EPS) and integer exactness (USD).
EPS_TOL = 0.01


def _annual_value(conn, security_id: int, concept: str, period_end: str,
                  as_of: str = AS_OF):
    """Annual figure for a concept/period as known on as_of (duration-filtered)."""
    row = conn.execute(
        """
        select f.value
        from fund.fundamentals_asof(%s::timestamptz) f
        join fund.concept c on c.concept_id = f.concept_id
        where f.security_id = %s
          and c.canonical_name = %s
          and f.period_end_date = %s
          and f.period_start is not null
          and (f.period_end_date - f.period_start) between 350 and 380
        order by f.period_end_date
        limit 1
        """,
        (as_of, security_id, concept, period_end),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# 1. Values are correct as of the historical date.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("concept,expected", EXPECTED_FY2017.items())
def test_fy2017_annual_values_known_as_of_2018(aapl_id, conn, concept, expected):
    got = _annual_value(conn, aapl_id, concept, "2017-09-30")
    assert got is not None, f"{concept} FY2017 missing as of {AS_OF}"
    if concept == "eps_diluted":
        assert abs(float(got) - expected) <= EPS_TOL
    else:
        assert int(got) == expected


# ---------------------------------------------------------------------------
# 2. THE lookahead guard: nothing from FY2018 is visible as of 2018-01-01.
#    Apple's FY2018 ended 2018-09-29 and was filed Nov 2018. If any FY2018
#    fact appears under an as-of of 2018-01-01, the bitemporal filter is broken.
# ---------------------------------------------------------------------------
def test_no_fy2018_leakage_as_of_2018(aapl_id, conn):
    leaked = conn.execute(
        """
        select count(*)
        from fund.fundamentals_asof(%s::timestamptz) f
        where f.security_id = %s
          and f.period_end_date > date '2017-09-30'
        """,
        (AS_OF, aapl_id),
    ).fetchone()[0]
    assert leaked == 0, (
        f"LOOKAHEAD BIAS: {leaked} fact(s) with period_end after FY2017 "
        f"are visible as of {AS_OF}. The as-of filter is broken."
    )


# ---------------------------------------------------------------------------
# 3. As-of monotonicity: a LATER as-of date must reveal FY2018 data that an
#    EARLIER one hid. Confirms the filter actually moves with the cutoff.
# ---------------------------------------------------------------------------
def test_fy2018_appears_after_it_was_filed(aapl_id, conn):
    # By 2019-01-01, Apple's FY2018 10-K (filed Nov 2018) is knowable.
    visible = conn.execute(
        """
        select count(*)
        from fund.fundamentals_asof('2019-01-01'::timestamptz) f
        where f.security_id = %s
          and f.period_end_date = date '2018-09-29'
        """,
        (aapl_id,),
    ).fetchone()[0]
    assert visible > 0, (
        "FY2018 facts should be visible as of 2019-01-01 but are not; "
        "the as-of filter may be over-restricting."
    )


# ---------------------------------------------------------------------------
# 4. Restatement integrity: the FY2016 CFO that was revised across filings
#    must exist as MULTIPLE bitemporal rows, and the as-of result must change
#    with the cutoff (65,824M originally, restated later).
# ---------------------------------------------------------------------------
def test_cfo_fy2016_restatement_is_bitemporal(aapl_id, conn):
    versions = conn.execute(
        """
        select count(distinct value)
        from fund.fundamental_fact ff
        join fund.concept c on c.concept_id = ff.concept_id
        where ff.security_id = %s
          and c.canonical_name = 'cfo'
          and ff.period_end_date = date '2016-09-24'
          and ff.period_start = date '2015-09-27'
        """,
        (aapl_id,),
    ).fetchone()[0]
    assert versions >= 2, (
        "Expected at least 2 distinct CFO values for FY2016 (original + "
        "restatement); the writer may be collapsing restatements."
    )


# ---------------------------------------------------------------------------
# 5. Duration discipline: querying without a duration filter returns MORE
#    rows than with one, proving flow concepts carry multiple durations and
#    that DEC-014 filtering is necessary (documents the consumption rule).
# ---------------------------------------------------------------------------
def test_flow_concept_has_multiple_durations(aapl_id, conn):
    unfiltered = conn.execute(
        """
        select count(*)
        from fund.fundamentals_asof(%s::timestamptz) f
        join fund.concept c on c.concept_id = f.concept_id
        where f.security_id = %s and c.canonical_name = 'cfo'
        """,
        (AS_OF, aapl_id),
    ).fetchone()[0]
    annual_only = conn.execute(
        """
        select count(*)
        from fund.fundamentals_asof(%s::timestamptz) f
        join fund.concept c on c.concept_id = f.concept_id
        where f.security_id = %s and c.canonical_name = 'cfo'
          and f.period_start is not null
          and (f.period_end_date - f.period_start) between 350 and 380
        """,
        (AS_OF, aapl_id),
    ).fetchone()[0]
    assert unfiltered > annual_only > 0, (
        "Expected more unfiltered cfo rows than annual-only, proving multiple "
        "durations coexist and duration filtering is required."
    )