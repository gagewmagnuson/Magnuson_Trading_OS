"""
Shared, source-independent validation for the EOD bars silver layer.

Repo path: src/trading_os/bars/validation.py

Audits one silver bar dataset, and compares a rebuilt dataset against the prior
one, before an atomic swap (DEC-024 rebuild; DEC-025 overlap validation). Reads
Parquet globs with DuckDB; no Postgres, no network. Reused by the Alpaca rebuild
now and by Tiingo overlap validation later — the audit is about the DATA, not the
source that produced it.

Five stages (the rebuild's proof of correctness):
  A. Row counts        — old vs rebuilt totals.
  B. Coverage parity   — every (security_id, session_date) that existed before
                         still exists (no bars silently lost).
  C. knowledge_time     — every rebuilt row's knowledge_time == the DEC-024
                         market-close derivation for its session_date. No exceptions.
  D. Duplicate audit   — exactly one row per (security_id, session_date). This is
                         THE check that the rebuild resolved the session-derived-
                         knowledge_time deduplication ambiguity: >1 means the read
                         path is back to non-deterministic tiebreaks.
  E. Sampled values    — random (security, session_date) sample; compare OHLCV
                         old vs rebuilt. Differences are surfaced (a vendor
                         revision superseded by latest-fetch-wins), not hidden.

The report is a kept artifact (Validator-A/B rigor, one dataset over). A rebuild
is not complete until D passes with zero duplicates and B loses zero coverage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import duckdb

from trading_os.bars.knowledge_time import market_close_knowledge_time


@dataclass
class ValueDiff:
    security_id: int
    session_date: date
    column: str
    old: object
    new: object


@dataclass
class ValidationReport:
    old_rows: int = 0
    new_rows: int = 0
    coverage_missing: int = 0            # (security, session) pairs lost in rebuild
    coverage_missing_samples: list[tuple[int, date]] = field(default_factory=list)
    knowledge_time_violations: int = 0   # rows whose kt != market_close(session_date)
    knowledge_time_samples: list[tuple[int, date]] = field(default_factory=list)
    duplicate_pairs: int = 0             # (security, session) pairs with >1 row
    duplicate_samples: list[tuple[int, date]] = field(default_factory=list)
    sampled: int = 0
    value_diffs: list[ValueDiff] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Hard gate: no lost coverage, no duplicates, no knowledge_time violations.
        Value diffs are surfaced for inspection but do NOT auto-fail (a rare vendor
        revision is legitimate under latest-fetch-wins)."""
        return (
            self.coverage_missing == 0
            and self.duplicate_pairs == 0
            and self.knowledge_time_violations == 0
        )

    def summary(self) -> str:
        lines = [
            "=== bars silver rebuild validation ===",
            f"A. rows:            old={self.old_rows:,}  new={self.new_rows:,}  "
            f"delta={self.new_rows - self.old_rows:+,}",
            f"B. coverage lost:   {self.coverage_missing:,} (security,session) pairs"
            + (f"  e.g. {self.coverage_missing_samples[:5]}" if self.coverage_missing_samples else ""),
            f"C. kt violations:   {self.knowledge_time_violations:,}"
            + (f"  e.g. {self.knowledge_time_samples[:5]}" if self.knowledge_time_samples else ""),
            f"D. duplicate pairs: {self.duplicate_pairs:,}"
            + (f"  e.g. {self.duplicate_samples[:5]}" if self.duplicate_samples else ""),
            f"E. sampled:         {self.sampled:,}  value diffs={len(self.value_diffs)}",
        ]
        for d in self.value_diffs[:10]:
            lines.append(f"     diff sec={d.security_id} {d.session_date} "
                         f"{d.column}: {d.old!r} -> {d.new!r}")
        if len(self.value_diffs) > 10:
            lines.append(f"     ... and {len(self.value_diffs) - 10} more diffs")
        lines.append(f"RESULT: {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def _con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    c.execute("SET TimeZone='UTC'")
    return c


def validate_rebuild(
    old_glob: str,
    new_glob: str,
    sample_size: int = 5000,
    seed: float = 0.42,
) -> ValidationReport:
    """Compare a rebuilt silver dataset (new_glob) against the prior one (old_glob).

    Both are Parquet globs. Runs stages A-E and returns a ValidationReport. Reads
    only Parquet; never mutates anything.
    """
    rep = ValidationReport()
    con = _con()
    try:
        old = f"read_parquet('{old_glob}')"
        new = f"read_parquet('{new_glob}')"

        # A. row counts
        rep.old_rows = con.execute(f"SELECT count(*) FROM {old}").fetchone()[0]
        rep.new_rows = con.execute(f"SELECT count(*) FROM {new}").fetchone()[0]

        # B. coverage parity — DISTINCT old pairs not present in new
        missing = con.execute(f"""
            SELECT o.security_id, o.session_date FROM (
                SELECT DISTINCT security_id, session_date FROM {old}
            ) o
            LEFT JOIN (
                SELECT DISTINCT security_id, session_date FROM {new}
            ) n USING (security_id, session_date)
            WHERE n.security_id IS NULL
        """).fetchall()
        rep.coverage_missing = len(missing)
        rep.coverage_missing_samples = [(r[0], r[1]) for r in missing[:20]]

        # D. duplicate audit — pairs with >1 row in the rebuilt set
        dups = con.execute(f"""
            SELECT security_id, session_date, count(*) c
            FROM {new} GROUP BY security_id, session_date HAVING count(*) > 1
        """).fetchall()
        rep.duplicate_pairs = len(dups)
        rep.duplicate_samples = [(r[0], r[1]) for r in dups[:20]]

        # C. knowledge_time audit — rebuilt kt must equal market_close(session_date).
        # Pull distinct (session_date, kt) pairs (cheap) and check in Python via the
        # canonical primitive, so the audit uses the exact same derivation as the write.
        kt_pairs = con.execute(f"""
            SELECT DISTINCT session_date, knowledge_time FROM {new}
        """).fetchall()
        bad_dates: set[date] = set()
        for sd, kt in kt_pairs:
            try:
                expected = market_close_knowledge_time(sd)
            except Exception:
                bad_dates.add(sd)
                continue
            if kt != expected:
                bad_dates.add(sd)
        if bad_dates:
            # count rows on the offending session_dates
            ph = ",".join("?" for _ in bad_dates)
            rep.knowledge_time_violations = con.execute(
                f"SELECT count(*) FROM {new} WHERE session_date IN ({ph})",
                list(bad_dates),
            ).fetchone()[0]
            rep.knowledge_time_samples = [(None, d) for d in list(bad_dates)[:20]]

        # E. sampled OHLCV comparison on shared pairs (reservoir = fixed count,
        # REPEATABLE = reproducible sample for a given seed)
        rows = con.execute(f"""
            SELECT o.security_id, o.session_date,
                   o.open, o.high, o.low, o.close, o.volume,
                   n.open, n.high, n.low, n.close, n.volume
            FROM (
                SELECT security_id, session_date, open, high, low, close, volume,
                       row_number() OVER (PARTITION BY security_id, session_date
                                          ORDER BY knowledge_time DESC) rn
                FROM {old}
            ) o
            JOIN {new} n USING (security_id, session_date)
                WHERE o.rn = 1
                USING SAMPLE reservoir({int(sample_size)} ROWS) REPEATABLE ({int(seed * 1000)})
            """).fetchall()
        rep.sampled = len(rows)
        cols = ["open", "high", "low", "close", "volume"]
        for r in rows:
            sec, sd = r[0], r[1]
            old_vals, new_vals = r[2:7], r[7:12]
            for name, ov, nv in zip(cols, old_vals, new_vals):
                if ov != nv:
                    rep.value_diffs.append(ValueDiff(sec, sd, name, ov, nv))
    finally:
        con.close()
    return rep