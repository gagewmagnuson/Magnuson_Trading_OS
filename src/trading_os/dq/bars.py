"""
Bars data-quality checks. Read-only over the silver Parquet lake (+ the XNYS
trading calendar in Postgres for cross-store checks). All evaluation of a single
bar uses the latest-known version per (security_id, session_date) so layered
bitemporal batches are never miscounted; the duplicate check deliberately scans
RAW rows, since its job is to catch true write-time duplication.
 
The gap logic lives in a pure function (compute_gaps) so the most error-prone
check is unit-testable with a synthetic calendar and no database.
"""
from __future__ import annotations
 
from dataclasses import dataclass, field
from datetime import date
 
from trading_os.engine.store import DuckDBStore
 
XNYS_MIC = "XNYS"
COVERAGE_FAIL_PCT = 99.0
FRESH_TOLERANCE_SESSIONS = 1  # 0-1 behind = current; >=2 = stale
 
 
# --------------------------- result shapes ---------------------------
@dataclass(frozen=True)
class CoverageReport:
    seeded: int
    with_bars: int
    missing: int
    coverage_pct: float
    missing_symbols: list[str]
 
 
@dataclass(frozen=True)
class SanityReport:
    high_lt_low: int
    ohlc_outside: int
    nonpositive: int
    total: int
 
    @property
    def violations(self) -> int:
        return self.high_lt_low + self.ohlc_outside + self.nonpositive
 
 
@dataclass(frozen=True)
class DuplicateReport:
    count: int
    examples: list[tuple] = field(default_factory=list)
 
 
@dataclass(frozen=True)
class ZeroVolumeReport:
    count: int
    examples: list[tuple] = field(default_factory=list)
 
 
@dataclass(frozen=True)
class GapReport:
    interior_by_security: list[tuple]   # (security_id, symbol, gap_count) desc
    interior_total: int
    listing_era_leak: int               # invariant: must be 0
 
 
@dataclass(frozen=True)
class FreshnessReport:
    lake_max: date | None
    calendar_max: date | None
    sessions_behind: int
    status: str                         # "current" | "stale" | "empty"
 
 
@dataclass
class DQResult:
    coverage: CoverageReport
    sanity: SanityReport
    duplicates: DuplicateReport
    zero_volume: ZeroVolumeReport
    gaps: GapReport
    freshness: FreshnessReport
    fail_reasons: list[str] = field(default_factory=list)
 
    @property
    def failed(self) -> bool:
        return bool(self.fail_reasons)
 
 
# --------------------------- helpers ---------------------------
def _glob(store: DuckDBStore) -> str:
    return (store.config.bars_eod_dir / "*.parquet").as_posix()
 
 
def _has_bars(store: DuckDBStore) -> bool:
    import glob as _g
    return bool(_g.glob(_glob(store)))
 
 
def _latest_cte(glob: str) -> str:
    """A CTE selecting the latest-known bar per (security_id, session_date)."""
    return f"""
    latest AS (
        SELECT *, row_number() OVER (PARTITION BY security_id, session_date
                                     ORDER BY knowledge_time DESC) AS rn
        FROM read_parquet('{glob}')
    )"""
 
 
def _xnys_exchange_id(store: DuckDBStore) -> int:
    row = store.con.execute(
        "SELECT exchange_id FROM pg.ref.exchange WHERE mic = ?", [XNYS_MIC]
    ).fetchone()
    if not row:
        raise RuntimeError(f"exchange {XNYS_MIC} not found in pg.ref.exchange")
    return row[0]
 
 
# --------------------------- pure gap logic ---------------------------
def compute_gaps(
    observed_by_security: dict[int, tuple[str, set[date]]],
    calendar: list[date],
) -> GapReport:
    """
    Per security, expected sessions are the calendar dates within that
    security's own [first_bar, last_bar] window; interior gaps are expected
    sessions with no bar. Per-security windowing means pre-listing sessions are
    never expected, so listing-era gaps are structurally impossible — the
    listing_era_leak counter re-checks that invariant (any gap earlier than the
    security's first bar) and must always be 0.
    """
    cal_sorted = sorted(calendar)
    per_sec: list[tuple] = []
    interior_total = 0
    listing_leak = 0
    for sid, (symbol, dates) in observed_by_security.items():
        if not dates:
            continue
        first, last = min(dates), max(dates)
        expected = [d for d in cal_sorted if first <= d <= last]
        missing = [d for d in expected if d not in dates]
        listing_leak += sum(1 for d in missing if d < first)  # structurally 0
        if missing:
            per_sec.append((sid, symbol, len(missing)))
            interior_total += len(missing)
    per_sec.sort(key=lambda r: r[2], reverse=True)
    return GapReport(per_sec, interior_total, listing_leak)
 
 
# --------------------------- checks ---------------------------
def coverage(store: DuckDBStore) -> CoverageReport:
    glob = _glob(store)
    seeded = store.con.execute("SELECT count(*) FROM pg.sec.security").fetchone()[0]
    if not _has_bars(store):
        return CoverageReport(seeded, 0, seeded, 0.0, [])
    with_bars = store.con.execute(
        f"SELECT count(DISTINCT security_id) FROM read_parquet('{glob}')"
    ).fetchone()[0]
    missing_rows = store.con.execute(
        f"""
        SELECT si.id_value
        FROM pg.sec.security s
        LEFT JOIN (SELECT DISTINCT security_id FROM read_parquet('{glob}')) b
               ON b.security_id = s.security_id
        JOIN pg.sec.security_identifier si
               ON si.security_id = s.security_id AND si.id_type = 'TICKER'
              AND si.valid_from <= current_date
              AND (si.valid_to IS NULL OR si.valid_to >= current_date)
        WHERE b.security_id IS NULL
        ORDER BY si.id_value
        """
    ).fetchall()
    missing = seeded - with_bars
    pct = round(100.0 * with_bars / seeded, 2) if seeded else 0.0
    return CoverageReport(seeded, with_bars, missing, pct, [r[0] for r in missing_rows])
 
 
def sanity(store: DuckDBStore) -> SanityReport:
    glob = _glob(store)
    if not _has_bars(store):
        return SanityReport(0, 0, 0, 0)
    row = store.con.execute(
        f"""
        WITH {_latest_cte(glob)}
        SELECT
            count(*) FILTER (WHERE high < low) AS high_lt_low,
            count(*) FILTER (WHERE close < low OR close > high
                                OR open  < low OR open  > high) AS ohlc_outside,
            count(*) FILTER (WHERE open<=0 OR high<=0 OR low<=0 OR close<=0) AS nonpos,
            count(*) AS total
        FROM latest WHERE rn = 1
        """
    ).fetchone()
    return SanityReport(row[0], row[1], row[2], row[3])
 
 
def duplicates(store: DuckDBStore) -> DuplicateReport:
    glob = _glob(store)
    if not _has_bars(store):
        return DuplicateReport(0, [])
    rows = store.con.execute(
        f"""
        SELECT security_id, session_date, knowledge_time, count(*) AS c
        FROM read_parquet('{glob}')
        GROUP BY 1, 2, 3 HAVING count(*) > 1
        ORDER BY c DESC LIMIT 10
        """
    ).fetchall()
    total = store.con.execute(
        f"""
        SELECT count(*) FROM (
            SELECT 1 FROM read_parquet('{glob}')
            GROUP BY security_id, session_date, knowledge_time HAVING count(*) > 1
        )
        """
    ).fetchone()[0]
    return DuplicateReport(total, rows)
 
 
def zero_volume(store: DuckDBStore) -> ZeroVolumeReport:
    glob = _glob(store)
    if not _has_bars(store):
        return ZeroVolumeReport(0, [])
    examples = store.con.execute(
        f"""
        WITH {_latest_cte(glob)}
        SELECT symbol, session_date FROM latest
        WHERE rn = 1 AND (volume IS NULL OR volume <= 0)
        ORDER BY session_date LIMIT 50
        """
    ).fetchall()
    total = store.con.execute(
        f"""
        WITH {_latest_cte(glob)}
        SELECT count(*) FROM latest
        WHERE rn = 1 AND (volume IS NULL OR volume <= 0)
        """
    ).fetchone()[0]
    return ZeroVolumeReport(total, examples)
 
 
def gaps(store: DuckDBStore) -> GapReport:
    glob = _glob(store)
    if not _has_bars(store):
        return GapReport([], 0, 0)
    ex_id = _xnys_exchange_id(store)
    gmin, gmax = store.con.execute(
        f"SELECT min(session_date), max(session_date) FROM read_parquet('{glob}')"
    ).fetchone()
    obs_rows = store.con.execute(
        f"""
        WITH {_latest_cte(glob)}
        SELECT security_id, any_value(symbol) AS symbol, list(session_date) AS dates
        FROM latest WHERE rn = 1
        GROUP BY security_id
        """
    ).fetchall()
    observed = {r[0]: (r[1], set(r[2])) for r in obs_rows}
    calendar = [
        r[0] for r in store.con.execute(
            """
            SELECT session_date FROM pg.ref.trading_session
            WHERE exchange_id = ? AND session_date BETWEEN ? AND ?
            ORDER BY session_date
            """,
            [ex_id, gmin, gmax],
        ).fetchall()
    ]
    return compute_gaps(observed, calendar)
 
 
def freshness(store: DuckDBStore) -> FreshnessReport:
    glob = _glob(store)
    if not _has_bars(store):
        return FreshnessReport(None, None, 0, "empty")
    ex_id = _xnys_exchange_id(store)
    lake_max = store.con.execute(
        f"SELECT max(session_date) FROM read_parquet('{glob}')"
    ).fetchone()[0]
    cal_max = store.con.execute(
        """
        SELECT max(session_date) FROM pg.ref.trading_session
        WHERE exchange_id = ? AND session_date <= current_date
        """,
        [ex_id],
    ).fetchone()[0]
    behind = store.con.execute(
        """
        SELECT count(*) FROM pg.ref.trading_session
        WHERE exchange_id = ? AND session_date > ? AND session_date <= current_date
        """,
        [ex_id, lake_max],
    ).fetchone()[0]
    status = "current" if behind <= FRESH_TOLERANCE_SESSIONS else "stale"
    return FreshnessReport(lake_max, cal_max, behind, status)
 
 
# --------------------------- orchestration ---------------------------
def run_all(store: DuckDBStore) -> DQResult:
    cov = coverage(store)
    san = sanity(store)
    dup = duplicates(store)
    zv = zero_volume(store)
    gp = gaps(store)
    fr = freshness(store)
 
    fail: list[str] = []
    if cov.seeded and cov.coverage_pct < COVERAGE_FAIL_PCT:
        fail.append(f"coverage {cov.coverage_pct}% < {COVERAGE_FAIL_PCT}%")
    if san.violations > 0:
        fail.append(f"OHLC sanity violations: {san.violations}")
    if dup.count > 0:
        fail.append(f"duplicate (security_id, session_date, knowledge_time): {dup.count}")
    if gp.listing_era_leak > 0:
        fail.append(f"listing-era gap leak: {gp.listing_era_leak} (DQ windowing bug)")
 
    return DQResult(cov, san, dup, zv, gp, fr, fail)