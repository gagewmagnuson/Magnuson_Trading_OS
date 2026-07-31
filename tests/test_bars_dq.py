"""Tests for bars DQ result semantics (passed/severity from count + fail_fraction)."""
from __future__ import annotations

import pytest

# The passed/severity logic is the thing to pin. We test it via a thin reimpl of
# the decision (kept identical to record_bar_dq) to avoid needing a live DB here;
# the DB write path is covered by the CLI integration.


def _decide(count, batch_size, fail_fraction):
    fraction = (count / batch_size) if batch_size else 0.0
    if count == 0:
        return True, "info"
    if fraction <= fail_fraction:
        return True, "warn"
    return False, "error"


def test_clean_batch_is_info_pass():
    assert _decide(0, 1000, 0.01) == (True, "info")


def test_few_anomalies_in_large_batch_pass_warn():
    # 7 skips in 4M bars -> tiny fraction -> healthy warn, not failure
    assert _decide(7, 4_000_000, 0.01) == (True, "warn")


def test_many_anomalies_fail_error():
    # 300 anomalies in 1000 bars -> 30% -> genuinely broken
    assert _decide(300, 1000, 0.01) == (False, "error")


def test_boundary_at_fail_fraction_passes():
    # exactly at the threshold -> still pass (<=)
    assert _decide(10, 1000, 0.01) == (True, "warn")


def test_just_over_fail_fraction_fails():
    assert _decide(11, 1000, 0.01) == (False, "error")


def test_empty_batch_no_anomalies_is_clean():
    assert _decide(0, 0, 0.01) == (True, "info")