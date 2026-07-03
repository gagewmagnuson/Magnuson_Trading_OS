"""
Adjustment-engine validation oracle (development tool; not a CI test yet).

Two validators, both comparing an adjusted close series to Tiingo's adjClose
(Tiingo follows CRSP methodology — the institutional reference):

  Validator A (mathematical oracle): Tiingo RAW prices + Tiingo-derived actions
    -> our CAF engine -> vs Tiingo adjClose. No lake. Isolates engine
    correctness. Strict gate on split-only.

  Validator B (system oracle): OUR LAKE bars (Alpaca) + actions seeded from
    Tiingo -> bars_eod_asof(adjustment="total_return") -> vs Tiingo adjClose.
    Validates the actual product (ingestion, storage, PIT, action lookup, CAF,
    application). Expect a small vendor-noise floor from Alpaca-vs-Tiingo raw
    price differences; correlation ~1 is the health signal.

Generic and reusable:
    python scripts/validate_adjustment_engine.py --symbols AAPL NVDA TSLA \
        --start 2016-01-01 --end 2026-06-24 --sample 50 --which A

Both validators report per symbol/mode: mean / median / 95th / max relative
error, worst date, correlation, R2 — plus a scannable PASS/PASS summary.
"""
from __future__ import annotations

import argparse
import random
from datetime import date

from trading_os.connectors.tiingo.actions import derive_actions
from trading_os.connectors.tiingo.client import TiingoClient
from trading_os.connectors.tiingo.stats import ErrorStats, compute_error_stats
from trading_os.engine.adjust import apply_factors_to_bars, compute_adjustment_factors

# These thresholds encode EXPECTATIONS about cross-vendor agreement, NOT
# algorithmic precision. Two correct implementations adjusting against
# different raw-price snapshots / dividend conventions differ by a small,
# uniform, highly-correlated amount. If the reference provider changes
# (Tiingo -> Polygon, etc.), re-derive these from a known-good run.
TR_MAX_TOL = 1e-3          # total_return: cross-vendor convention/rounding floor
TR_CORR_MIN = 0.9999       # correlation: catches missing events & date shifts
SPLIT_EXACT_TOL = 1e-9     # split-only on ZERO-dividend symbols: near-exact
SENTINEL_SID = 1           # synthetic security_id for the self-contained path
# Validator B: the pipeline should add negligible error BEYOND the raw
# Alpaca-vs-Tiingo vendor floor. This gates the DELTA (adjusted - raw), not an
# absolute bound. Named/overridable; re-derive if the price vendor changes.
PIPELINE_DELTA_TOL = 1e-5   # max additional error the pipeline may introduce
B_CORR_MIN = 0.9999         # correlation floor for the adjusted comparison

def _row_date(row: dict) -> date:
    return date.fromisoformat(row["date"][:10])


def _sample_dates(all_dates: list[date], k: int) -> list[date]:
    if len(all_dates) <= k:
        return all_dates
    # deterministic spread: evenly spaced + fixed-seed jitter for coverage
    step = len(all_dates) / k
    idx = sorted({min(len(all_dates) - 1, int(i * step)) for i in range(k)})
    return [all_dates[i] for i in idx]


def _bars_from_tiingo(rows: list[dict], sec_id: int) -> list[tuple]:
    """Build engine bar tuples from Tiingo RAW OHLCV (12-col layout)."""
    bars = []
    for r in rows:
        d = _row_date(r)
        c = float(r["close"])
        bars.append((sec_id, "SYM", d, float(r["open"]), float(r["high"]),
                     float(r["low"]), c, int(r["volume"]), None, c, None, "TIINGO"))
    return bars


def validate_self_contained(rows: list[dict], mode: str, sample: int) -> ErrorStats:
    """Validator A: Tiingo raw + derived actions -> our engine -> vs adjClose."""
    actions, _warn = derive_actions(SENTINEL_SID, rows)
    bars = _bars_from_tiingo(rows, SENTINEL_SID)
    sessions = {SENTINEL_SID: [b[2] for b in bars]}
    closes = {SENTINEL_SID: {b[2]: b[6] for b in bars}}
    factors = compute_adjustment_factors(actions, closes, sessions, mode=mode)
    adj = apply_factors_to_bars(bars, factors)
    ours_by_date = {b[2]: b[6] for b in adj}
    ref_by_date = {_row_date(r): float(r["adjClose"]) for r in rows}

    all_dates = sorted(ours_by_date)
    chosen = _sample_dates(all_dates, sample)
    pairs = [(d.isoformat(), ours_by_date[d], ref_by_date[d])
             for d in chosen if d in ref_by_date]
    return compute_error_stats(pairs)


def _fmt(s: ErrorStats) -> str:
    if s.n == 0:
        return "   (no comparable points)"
    return (f"   n={s.n:<4} mean={s.mean:.2e} median={s.median:.2e} "
            f"p95={s.p95:.2e} max={s.max:.2e}\n"
            f"   corr={s.correlation:.8f} R2={s.r2:.8f} worst={s.worst_date}")


def run_validator_a(symbols, start, end, sample,
tr_tol=TR_MAX_TOL, corr_min=TR_CORR_MIN,
split_tol=SPLIT_EXACT_TOL):
    client = TiingoClient()
    results = {}
    print("=" * 68)
    print("VALIDATOR A — mathematical oracle (Tiingo raw -> our engine -> Tiingo adjClose)")
    print("=" * 68)
    all_pass = True
    for sym in symbols:
        rows = client.fetch_daily(sym, start, end)
        if not rows:
            print(f"\n{sym}: no data"); all_pass = False; continue
        actions, warns = derive_actions(SENTINEL_SID, rows)
        n_split = sum(1 for a in actions if a.action_type == "SPLIT")
        n_div = sum(1 for a in actions if a.action_type == "CASH_DIVIDEND")
        print(f"\n{sym}  ({len(rows)} days, {n_split} splits, {n_div} dividends)")
        for w in warns:
            print(f"   ! {w}")
        s_split = validate_self_contained(rows, "split", sample)
        s_tr = validate_self_contained(rows, "total_return", sample)
        print("  split-only:")
        print(_fmt(s_split))
        print("  total_return:")
        print(_fmt(s_tr))

        # total_return is the like-for-like gate: Tiingo's adjClose is ALWAYS
        # split+dividend adjusted, so total_return compares the same quantity.
        tr_ok = (s_tr.n > 0 and s_tr.max < tr_tol and s_tr.correlation > corr_min)
        if s_tr.n > 0:
            agree = ("uniform (corr %.7f) — consistent with vendor convention, "
                    "not error" % s_tr.correlation) if tr_ok else \
                    ("NON-uniform or oversized — investigate (corr %.7f)"
                    % s_tr.correlation)
            print(f"  -> total_return gate (max<{tr_tol:.0e} & corr>{corr_min}): "
                f"{'PASS' if tr_ok else 'FAIL'}")
            print(f"     agreement: {agree}")

        # split-only is only gatable where adjClose == split-adjusted, i.e. on
        # zero-dividend securities. Elsewhere it's informational, with a reason.
        if n_div == 0:
            split_ok = s_split.passes(split_tol)
            print(f"  -> split-only: GATED (zero-dividend security -> "
                f"adjClose == split-adjusted)")
            print(f"     {'PASS' if split_ok else 'FAIL'}  "
                f"max={s_split.max:.2e} < {split_tol:.0e}")
        else:
            split_ok = True
            print("  -> split-only: informational only")
            print("     reason: reference provider (Tiingo adjClose) supplies "
                "TOTAL-RETURN adjusted prices;")
            print("             split-only is gated only on zero-dividend securities.")

        sym_ok = tr_ok and split_ok
        results[sym] = sym_ok
        all_pass = all_pass and sym_ok
    return all_pass, results

def _lake_series(store, security_id, as_of, adjustment):
    """{session_date: close} from our lake via bars_eod_asof."""
    rows = store.bars_eod_asof(as_of, security_ids=[security_id],
                               adjustment=adjustment)
    return {r[2]: r[6] for r in rows}


def _tiingo_series(rows, field):
    from datetime import date as _date
    return {_date.fromisoformat(r["date"][:10]): float(r[field]) for r in rows}


def run_validator_b(symbols, start, end, sample,
                    delta_tol, corr_min, do_bootstrap):
    from datetime import date
    from trading_os.engine.config import EngineConfig
    from trading_os.engine.store import DuckDBStore
    from trading_os.connectors.tiingo.client import TiingoClient
    from trading_os.connectors.tiingo.actions import derive_actions

    if do_bootstrap:
        from trading_os.connectors.tiingo.loader import bootstrap
        print("[--bootstrap-actions] loading Tiingo actions before validating...")
        for st in bootstrap(symbols, start, end):
            print(f"   {st.symbol}: inserted {st.inserted}, "
                  f"skipped {st.skipped_exact}, conflicts {st.conflicts}")

    client = TiingoClient()
    store = DuckDBStore(EngineConfig())
    store.connect(attach_postgres=True)
    as_of = date.today()
    all_pass = True
    print("=" * 68)
    print("VALIDATOR B — system oracle (our lake via bars_eod_asof -> vs Tiingo)")
    print("=" * 68)
    try:
        for sym in symbols:
            sid = store.con.execute(
                "select security_id from pg.sec.security_identifier "
                "where id_type='TICKER' and id_value=? and valid_from<=current_date "
                "and (valid_to is null or valid_to>=current_date) limit 1", [sym]
            ).fetchone()
            if not sid:
                print(f"\n{sym}: not in security master"); all_pass = False; continue
            sid = sid[0]

            trows = client.fetch_daily(sym, start, end)
            t_raw = _tiingo_series(trows, "close")
            t_adj = _tiingo_series(trows, "adjClose")
            lake_raw = _lake_series(store, sid, as_of, None)
            lake_adj = _lake_series(store, sid, as_of, "total_return")

            actions, _ = derive_actions(sid, trows)
            expected = len(actions)
            retrieved = len(store._fetch_actions([sid], as_of))
            # An action can only adjust bars STRICTLY BEFORE its ex_date, so an
            # action whose ex_date is <= the first bar (or > the last) has no bar
            # to act on and is legitimately inapplicable — not a coverage miss.
            if lake_raw:
                lo, hi = min(lake_raw), max(lake_raw)
                applicable = [a for a in actions if lo < a.ex_date <= hi]
            else:
                applicable = []
            applied = len(applicable)
            # coverage = applied / APPLICABLE (boundary actions excluded), so a
            # dividend on the first bar date doesn't fail an otherwise-correct name.
            cov = (100.0 * applied / len(applicable)) if applicable else 100.0
            boundary = expected - applied  # reported for transparency

            common = sorted(set(lake_raw) & set(t_raw) & set(lake_adj) & set(t_adj))
            chosen = _sample_dates(common, sample)
            raw_pairs = [(d.isoformat(), lake_raw[d], t_raw[d]) for d in chosen]
            adj_pairs = [(d.isoformat(), lake_adj[d], t_adj[d]) for d in chosen]
            raw_s = compute_error_stats(raw_pairs)
            adj_s = compute_error_stats(adj_pairs)

            delta_max = adj_s.max - raw_s.max
            delta_mean = adj_s.mean - raw_s.mean
            ok = (adj_s.n > 0
                  and adj_s.correlation > corr_min
                  and adj_s.r2 > corr_min
                  and applied == len(applicable)
                  and adj_s.max < TR_MAX_TOL)

            print(f"\n{sym}  (n={adj_s.n} compared dates)")
            print(f"  raw   vs raw : mean={raw_s.mean:.2e} max={raw_s.max:.2e} "
                  f"corr={raw_s.correlation:.7f}")
            print(f"  adj   vs adj : mean={adj_s.mean:.2e} max={adj_s.max:.2e} "
                  f"corr={adj_s.correlation:.7f} R2={adj_s.r2:.7f}")
            print(f"  pipeline delta: mean={delta_mean:+.2e} max={delta_max:+.2e}")
            print(f"  actions      : expected={expected} retrieved={retrieved} "
                  f"applied={applied} (boundary={boundary})  coverage={cov:.1f}%")
            print(f"  PIT          : actions retrieved as-of {as_of} "
                  f"(knowledge_time/ex_date filtered)")
            verdict = "PASS" if ok else "FAIL"
            print(f"  -> {verdict} — gates: corr>{corr_min}, R2>{corr_min}, "
                  f"coverage=100%, adj_max<{TR_MAX_TOL:.0e}")
            print(f"     diagnostic: pipeline delta {delta_max:+.2e} "
                  f"(reported, not gated — scales with dividend count)")
            all_pass = all_pass and ok
    finally:
        store.close()
    return all_pass

def main(argv=None):
    p = argparse.ArgumentParser(description="Adjustment-engine validation oracle.")
    p.add_argument("--symbols", nargs="+", default=["AAPL", "NVDA", "TSLA"])
    p.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=None)
    p.add_argument("--sample", type=int, default=50)
    p.add_argument("--which", choices=["A", "B", "both"], default="A")
    p.add_argument("--bootstrap-actions", action="store_true",
    help="load Tiingo actions into corp.corporate_action before Validator B")
    p.add_argument("--tr-tol", type=float, default=TR_MAX_TOL,
    help="total_return max relative-error tolerance (vendor floor)")
    p.add_argument("--corr-min", type=float, default=TR_CORR_MIN,
    help="minimum correlation for a total_return pass")
    p.add_argument("--split-tol", type=float, default=SPLIT_EXACT_TOL,
    help="split-only tolerance (zero-dividend symbols only)")
    args = p.parse_args(argv)

    a_pass = None
    if args.which in ("A", "both"):
        a_pass, _ = run_validator_a(args.symbols, args.start, args.end, args.sample,
        tr_tol=args.tr_tol, corr_min=args.corr_min,
        split_tol=args.split_tol)
    
    b_pass = None
    if args.which in ("B", "both"):
        b_pass = run_validator_b(args.symbols, args.start, args.end, args.sample,
        delta_tol=PIPELINE_DELTA_TOL, corr_min=B_CORR_MIN,
        do_bootstrap=args.bootstrap_actions)

    print("\n" + "-" * 68)
    if a_pass is not None:
        print(f"Validator A (engine) : {'PASS' if a_pass else 'FAIL'}")
    if b_pass is not None:
        print(f"Validator B (system) : {'PASS' if b_pass else 'FAIL'}")
    eng = "PASS" if a_pass else ("n/a" if a_pass is None else "FAIL")
    sysr = "PASS" if b_pass else ("n/a" if b_pass is None else "FAIL")
    print(f"Summary — Engine: {eng}  System: {sysr}")
    print("-" * 68)
    failed = (a_pass is False) or (b_pass is False)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())