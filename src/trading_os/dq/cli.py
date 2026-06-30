"""
Bars data-quality CLI. Read-only; prints a tiered report and exits non-zero iff
a FAIL-tier check tripped (coverage < 99%, OHLC violations, duplicate PIT keys,
or a listing-era gap leak). REPORT-tier findings (interior gaps, zero-volume,
freshness) print but never fail the run.

Usage (repo root, venv active, Postgres reachable):
    PYTHONPATH=src python -m trading_os.dq.cli
    PYTHONPATH=src python -m trading_os.dq.cli --gaps-limit 30
"""
from __future__ import annotations

import argparse
import sys

from trading_os.engine.store import DuckDBStore

from . import bars as dq


def _print_report(r: dq.DQResult, gaps_limit: int) -> None:
    c = r.coverage
    print("=" * 64)
    print("BARS DATA QUALITY")
    print("=" * 64)
    print(f"Coverage : {c.seeded} seeded, {c.with_bars} with bars, "
          f"{c.missing} missing ({c.coverage_pct}%)")
    if c.missing_symbols:
        shown = ", ".join(c.missing_symbols[:20])
        more = "" if len(c.missing_symbols) <= 20 else f" (+{len(c.missing_symbols)-20} more)"
        print(f"           missing: {shown}{more}")

    s = r.sanity
    print(f"Sanity   : {s.violations} violations "
          f"(high<low={s.high_lt_low}, ohlc_outside={s.ohlc_outside}, "
          f"nonpositive={s.nonpositive}) over {s.total} bars")

    d = r.duplicates
    print(f"Duplicates: {d.count} duplicate PIT keys")
    for sid, sd, kt, cnt in d.examples[:5]:
        print(f"           security_id={sid} {sd} {kt} x{cnt}")

    g = r.gaps
    print(f"Gaps     : {g.interior_total} interior session gaps across "
          f"{len(g.interior_by_security)} securities; "
          f"listing-era leak={g.listing_era_leak}")
    for sid, sym, cnt in g.interior_by_security[:gaps_limit]:
        print(f"           {sym} (security_id={sid}): {cnt}")

    z = r.zero_volume
    print(f"Volume   : {z.count} zero/null-volume bars")
    for sym, sd in z.examples[:10]:
        print(f"           {sym} {sd}")

    f = r.freshness
    print(f"Freshness: lake max {f.lake_max}, latest XNYS {f.calendar_max} -> "
          f"{f.status} ({f.sessions_behind} behind)")

    print("-" * 64)
    if r.failed:
        print("RESULT   : FAIL")
        for reason in r.fail_reasons:
            print(f"           - {reason}")
    else:
        print("RESULT   : PASS")
    print("=" * 64)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bars data-quality report (read-only).")
    p.add_argument("--gaps-limit", type=int, default=20,
                   help="max securities to list in the interior-gaps section")
    args = p.parse_args(argv)

    store = DuckDBStore()
    store.connect(attach_postgres=True)  # coverage/gaps/freshness are cross-store
    try:
        result = dq.run_all(store)
    finally:
        store.close()

    _print_report(result, args.gaps_limit)
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())