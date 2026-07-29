"""
Corroborate delisted candidates by historical-identity plausibility (local, no re-fetch).

Repo path: src/trading_os/universe/corroborate.py
Run:  PYTHONPATH=src python -m trading_os.universe.corroborate

Ticker lookup is NOT identity lookup. Tiingo answers "who holds ticker X today,"
which for a dead ticker is often a DIFFERENT company that reused it (ABC ->
Adbri, ADS -> a GraniteShares ETF). This pass separates "Tiingo is covering the
historical company" from "Tiingo is describing a modern reuser," using the one
reliable signal available locally: does Tiingo's coverage INTERVAL overlap the
window the company was actually in the S&P?

  Bucket A (add):    [tiingo_start, tiingo_end] overlaps [sp500_first, sp500_last]
                     -> coverage plausibly belongs to the historical company.
  Bucket B (review): tiingo_start is after sp500_last -> coverage begins after the
                     company was gone -> modern ticker reuse. Human adjudicates.
  Bucket C (review): blank Tiingo window -> nothing to corroborate.

Deliberately NOT used as signals:
  - end-date vs sp500 exit distance: S&P exit != delisting; a company may trade
    for years after leaving the index. Comparing them creates false reviews.
  - CIK presence: only means the issuer exists in SEC's CURRENT database; carries
    no reliable reuse signal. Useful for manual investigation, not scoring.

Overlap is strong, not perfect: a ticker reused by a company whose window happens
to overlap could still slip through. That residual goes to the same human REVIEW
as the original ambiguous cases — the goal is to shrink the review pile to the
genuinely uncertain, not to eliminate human judgment. This is ONE of two
independent gates: this classifier judges historical plausibility; the writer's
collision guard (separately) protects the live master. Both must pass.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

CLASSIFIER_CSV = Path("classify_delisted_report.csv")
OUT_CSV = Path("corroborated_report.csv")


def _d(s: str | None) -> date | None:
    s = (s or "").strip()
    return date.fromisoformat(s) if s else None


def _overlaps(t_start: date | None, t_end: date | None,
              m_start: date | None, m_end: date | None) -> bool:
    """Do the Tiingo coverage interval and the S&P membership interval overlap?
    Open Tiingo end (still trading) -> treat as far-future. Missing membership
    bounds -> treated permissively (they exist for every sp500 row in practice)."""
    if t_start is None:
        return False                       # no coverage start -> cannot corroborate
    hi_t = t_end or date.max               # still-trading -> open upper bound
    lo_m = m_start or date.min
    hi_m = m_end or date.max
    # standard interval overlap: t_start <= hi_m AND lo_m <= hi_t
    return t_start <= hi_m and lo_m <= hi_t


def corroborate() -> dict[str, int]:
    if not CLASSIFIER_CSV.exists():
        raise SystemExit(f"missing {CLASSIFIER_CSV}; run classify_delisted first.")
    rows = list(csv.DictReader(CLASSIFIER_CSV.open()))

    out_rows: list[dict] = []
    counts: dict[str, int] = {}
    for r in rows:
        rec = r["recommendation"]
        # Only re-examine the rows that were heading toward 'add'. REVIEW /
        # unavailable / translate keep their existing disposition (REVIEW is
        # already human-adjudicated via the overrides; unavailable has no data;
        # translate is a known dot-class add with a real window).
        if rec != "add":
            verdict = f"passthrough:{rec}"
        else:
            t_start, t_end = _d(r["tiingo_start"]), _d(r["tiingo_end"])
            m_start, m_end = _d(r["sp500_first"]), _d(r["sp500_last"])
            if t_start is None:
                verdict = "review:blank_window"
            elif _overlaps(t_start, t_end, m_start, m_end):
                verdict = "add:overlap_ok"
            else:
                verdict = "review:modern_reuse"   # tiingo coverage begins after S&P exit
        counts[verdict] = counts.get(verdict, 0) + 1
        out_rows.append({**r, "corroboration": verdict})

    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    print("=== corroboration (historical-identity overlap) ===")
    for k in sorted(counts):
        print(f"  {k:26} {counts[k]}")
    add_ct = counts.get("add:overlap_ok", 0)
    rev_ct = counts.get("review:modern_reuse", 0) + counts.get("review:blank_window", 0)
    print(f"\n  auto-add (overlap OK):    {add_ct}")
    print(f"  -> REVIEW (reuse/blank):  {rev_ct}")
    print(f"  report -> {OUT_CSV.resolve()}")
    return counts


if __name__ == "__main__":
    raise SystemExit(corroborate())