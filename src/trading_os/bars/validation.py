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
    price_tol: float = 0.0
    price_diffs: list[ValueDiff] = field(default_factory=list)   # OHLC beyond tol — should be ~0
    volume_diffs: list[ValueDiff] = field(default_factory=list)  # volume mismatches — often expected
    volume_rel_median: float | None = None   # median |Δvol|/old_vol; ~0.09 = the definitional pattern

    @property
    def passed(self) -> bool:
        """Hard gate: no lost coverage, no duplicates, no knowledge_time violations.
        Value diffs (price or volume) are surfaced for inspection but do NOT
        auto-fail — a rare vendor revision, and the expected volume-convention
        difference, are both legitimate. A human reads price_diffs and decides."""
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
            f"E. sampled:         {self.sampled:,}  (price tol {self.price_tol:.2%})",
            f"   PRICE diffs:     {len(self.price_diffs):,}  <- should be ~0; scrutinize each",
            f"   volume diffs:    {len(self.volume_diffs):,}"
            + (f"  median |Δ|/old={self.volume_rel_median:.1%}" if self.volume_rel_median is not None else "")
            + "  (expected: Tiingo exchange-cleared vs consolidated SIP)",
        ]
        for d in self.price_diffs[:10]:
            lines.append(f"     PRICE sec={d.security_id} {d.session_date} "
                         f"{d.column}: {d.old!r} -> {d.new!r}")
        if len(self.price_diffs) > 10:
            lines.append(f"     ... and {len(self.price_diffs) - 10} more price diffs")
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
    price_tol: float = 0.001,   # 0.1% relative — ignore sub-cent rounding, flag real disagreement
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

        # E. sampled comparison on shared pairs — PRICE and VOLUME reported
        # separately. Price diffs beyond price_tol are the ones to scrutinize
        # (should be ~0). Volume diffs are counted separately because Tiingo's
        # exchange-cleared volume runs ~8-11% below consolidated-SIP (Alpaca) by
        # definition, not error — burying price signal under them would defeat
        # the check. (reservoir = fixed count, REPEATABLE = reproducible sample.)
        rep.price_tol = price_tol
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
        price_cols = ["open", "high", "low", "close"]
        vol_rels: list[float] = []
        for r in rows:
            sec, sd = r[0], r[1]
            old_ohlc, old_vol = r[2:6], r[6]
            new_ohlc, new_vol = r[7:11], r[11]
            for name, ov, nv in zip(price_cols, old_ohlc, new_ohlc):
                if ov is None or nv is None:
                    if ov != nv:
                        rep.price_diffs.append(ValueDiff(sec, sd, name, ov, nv))
                elif abs(ov - nv) > price_tol * max(abs(ov), abs(nv), 1e-9):
                    rep.price_diffs.append(ValueDiff(sec, sd, name, ov, nv))
            if old_vol != new_vol:
                rep.volume_diffs.append(ValueDiff(sec, sd, "volume", old_vol, new_vol))
            if old_vol and new_vol is not None:
                vol_rels.append(abs(old_vol - new_vol) / old_vol)
        if vol_rels:
            vol_rels.sort()
            rep.volume_rel_median = vol_rels[len(vol_rels) // 2]
    finally:
        con.close()
    return rep