"""
Persist bars data-quality results to meta.dq_result (one summary row per batch).

Repo path: src/trading_os/bars/dq.py

The bars write path collects anomalies as structured objects (writer.SkippedBar
for non-session dates; connector ParseAnomaly for malformed rows). This module
records ONE summary dq_result per check per batch — a batch health record, not an
anomaly log. Individual anomaly objects stay in memory / logs; dq_result answers
"was batch N healthy, and how many anomalies of type X did it have."

Semantics (per the DQ design):
  passed   = the batch satisfies acceptable quality. A handful of anomalies in a
             large batch is healthy (passed=true, warn) — a detector that fires is
             succeeding, not failing. Only a large SHARE fails the batch.
  severity = 'info'  (count 0) | 'warn' (some, within policy) | 'error' (fails).
  observed = {"count", "batch_size", "fraction", "sample"} (sample capped).

The acceptance threshold (fail_fraction) is read from the check's spec — policy is
data, not hardcoded here.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

_SAMPLE_CAP = 20   # cap the anomaly sample stored in observed jsonb


@dataclass(frozen=True)
class DQCheck:
    check_id: int
    name: str
    fail_fraction: float


def load_check(conn: psycopg.Connection, name: str) -> DQCheck:
    """Look up a registered check by name; read its fail_fraction from spec."""
    row = conn.execute(
        "select check_id, coalesce((spec->>'fail_fraction')::float, 1.0) "
        "from meta.data_quality_check where name = %s and enabled",
        (name,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no enabled data_quality_check named {name!r} "
                         f"(seed it via a migration first)")
    return DQCheck(check_id=row[0], name=name, fail_fraction=row[1])


def record_bar_dq(
    conn: psycopg.Connection,
    check_name: str,
    batch_id: int,
    anomaly_count: int,
    batch_size: int,
    sample: list[str],
) -> dict:
    """Write ONE summary dq_result for (check, batch). Returns the observed dict.

    passed/severity from the count and the check's fail_fraction:
      count == 0                          -> passed=true,  severity=info
      0 < fraction <= fail_fraction       -> passed=true,  severity=warn
      fraction  > fail_fraction           -> passed=false, severity=error
    (batch_size 0 -> fraction 0, treated as clean.)
    """
    check = load_check(conn, check_name)
    fraction = (anomaly_count / batch_size) if batch_size else 0.0

    if anomaly_count == 0:
        passed, severity = True, "info"
    elif fraction <= check.fail_fraction:
        passed, severity = True, "warn"
    else:
        passed, severity = False, "error"

    observed = {
        "count": anomaly_count,
        "batch_size": batch_size,
        "fraction": round(fraction, 6),
        "severity": severity,
        "sample": sample[:_SAMPLE_CAP],
    }
    details = (f"{check_name}: {anomaly_count} anomalies in {batch_size} bars "
               f"({fraction:.4%}); {'PASS' if passed else 'FAIL'} ({severity})")

    conn.execute(
        """
        insert into meta.dq_result (check_id, batch_id, passed, observed, details)
        values (%s, %s, %s, %s, %s)
        """,
        (check.check_id, batch_id, passed, psycopg.types.json.Json(observed), details),
    )
    return observed