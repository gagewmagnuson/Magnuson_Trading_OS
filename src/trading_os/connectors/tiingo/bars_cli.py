"""
Tiingo EOD bars backfill CLI (DEC-025 / DEC-026).

Repo path: src/trading_os/connectors/tiingo/bars_cli.py
Run:
  build+validate (no swap):  python -m trading_os.connectors.tiingo.bars_cli
  test a few tickers first:  python -m trading_os.connectors.tiingo.bars_cli --limit 5
  swap after review:         python -m trading_os.connectors.tiingo.bars_cli --swap

Tiingo is the canonical EOD source. This fetches full available history per
security (no history floor -> the 1900 sentinel means "everything the vendor has,
per each security's IPO"; Tiingo caps each response at the security's first
trading day), writes RAW OHLCV to STAGING silver via the shared DEC-024 writer,
and validates staging against current live silver — which doubles as the DEC-025
Alpaca-vs-Tiingo overlap comparison.

Build and swap are SEPARATE so the expensive fetch runs once: the default run
builds staging + validates + reports; --swap validates the EXISTING staging and
atomically swaps it in (no re-fetch). Build reads only Tiingo + bronze; live
silver is only ever renamed to a backup on swap.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

from trading_os.bars.lineage import (
    SourceSpec,
    all_seeded_symbols,
    close_batch,
    ensure_source,
    open_batch,
    resolve_security_ids,
)
from trading_os.bars.models import Bar
from trading_os.bars.silver_store import (
    clear_staging,
    silver_glob,
    staging_dir,
    swap_staging_into_live,
)
from trading_os.bars.validation import validate_rebuild
from trading_os.bars.writer import write_bars_parquet
from trading_os.config import settings

from .bars import ParseAnomaly, parse_bars
from .client import TiingoClient
from .config import TiingoConfig

# No history floor (DEC-025/026). The Tiingo API requires a startDate, so we pass a
# sentinel far below any security's IPO. This is NOT a policy cutoff and NOT a claim
# that 1900 data exists — it means "give me everything you have for this ticker."
# Tiingo caps each response at the security's actual first trading day. Do NOT
# "tidy" this into a real-looking date; that silently reintroduces a floor.
BARS_HISTORY_SENTINEL = date(1900, 1, 1)

TIINGO_BARS_SPEC = SourceSpec(
    name="TIINGO",
    kind="prices",
    is_redistributable=False,
    base_url="https://api.tiingo.com",
    license_notes=(
        "Tiingo (Power plan). EOD OHLCV incl. delisted securities. Raw "
        "redistribution restricted per Tiingo terms; derived data use permitted."
    ),
)


def _silver_dir(config: TiingoConfig) -> Path:
    return config.lake_root / "silver" / "bars_eod"


@dataclass
class BuildResult:
    requested: int
    resolved: int
    fetched_ok: int
    fetch_failures: list[tuple[str, str]]      # (symbol, error)
    parse_anomalies: list[ParseAnomaly]
    total_bars: int
    rows_written: int
    writer_skipped: int
    batch_id: int


def build_staging(
    conn: psycopg.Connection,
    config: TiingoConfig,
    client: TiingoClient,
    requested: list[str],
) -> BuildResult:
    sec_map = resolve_security_ids(conn, requested)
    staging = clear_staging(_silver_dir(config))

    ingest_kt = datetime.now(timezone.utc)
    source_id = ensure_source(conn, TIINGO_BARS_SPEC)
    batch_id = open_batch(
        conn, source_id, ingest_kt,
        {"backfill": True, "requested": len(requested), "resolved": len(sec_map),
         "start": BARS_HISTORY_SENTINEL.isoformat()},
        code_version="tiingo-bars-v1",
    )

    all_bars: list[Bar] = []
    parse_anomalies: list[ParseAnomaly] = []
    fetch_failures: list[tuple[str, str]] = []
    fetched_ok = 0

    items = sorted(sec_map.items())
    total = len(items)
    try:
        for i, (symbol, sec_id) in enumerate(items, 1):
            try:
                rows = client.fetch_daily(symbol, BARS_HISTORY_SENTINEL)
            except Exception as e:  # noqa: BLE001 — one bad ticker must not kill the run
                fetch_failures.append((symbol, str(e)[:160]))
            else:
                bars, anomalies = parse_bars(sec_id, symbol, rows)
                all_bars.extend(bars)
                parse_anomalies.extend(anomalies)
                fetched_ok += 1
            if i % 50 == 0 or i == total:
                print(f"[tiingo] fetched {i}/{total} tickers "
                      f"({fetched_ok} ok, {len(fetch_failures)} failed, "
                      f"{len(all_bars):,} bars)", flush=True)
            time.sleep(config.request_interval)   # pace under the rate limit

        result = write_bars_parquet(all_bars, staging, "TIINGO", batch_id)
        close_batch(conn, batch_id, "succeeded",
                    rows_in=len(all_bars), rows_out=result.rows_written)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        close_batch(conn, batch_id, "failed", rows_in=len(all_bars), rows_out=0,
                    error=str(e))
        conn.commit()
        raise

    return BuildResult(
        requested=len(requested), resolved=len(sec_map), fetched_ok=fetched_ok,
        fetch_failures=fetch_failures, parse_anomalies=parse_anomalies,
        total_bars=len(all_bars), rows_written=result.rows_written,
        writer_skipped=len(result.skipped), batch_id=batch_id,
    )


def _print_build_report(b: BuildResult) -> None:
    print(f"\n[tiingo] === backfill build report (batch {b.batch_id}) ===")
    print(f"  requested={b.requested}  resolved={b.resolved}  "
          f"fetched_ok={b.fetched_ok}  fetch_failures={len(b.fetch_failures)}")
    print(f"  bars parsed={b.total_bars:,}  written={b.rows_written:,}  "
          f"writer_skipped={b.writer_skipped}  parse_anomalies={len(b.parse_anomalies)}")
    for sym, err in b.fetch_failures[:10]:
        print(f"    FETCH FAIL {sym}: {err}")
    if len(b.fetch_failures) > 10:
        print(f"    ... and {len(b.fetch_failures) - 10} more fetch failures")
    for a in b.parse_anomalies[:10]:
        print(f"    PARSE {a.symbol} {a.session_date} — {a.reason}")
    if len(b.parse_anomalies) > 10:
        print(f"    ... and {len(b.parse_anomalies) - 10} more parse anomalies")


def _record_override(silver_dir: Path, report, reason: str) -> Path:
    """Append an explicit human override of a failing validation gate to a durable
    log beside the silver lake. The gate itself is never weakened — overrides are
    recorded so a future reader can see exactly what was accepted, when, and why."""
    log = silver_dir.parent / "swap_overrides.log"
    with log.open("a") as f:
        f.write(
            f"{datetime.now(timezone.utc).isoformat()}  silver={silver_dir.name}\n"
            f"  coverage_missing={report.coverage_missing}  "
            f"duplicate_pairs={report.duplicate_pairs}  "
            f"kt_violations={report.knowledge_time_violations}\n"
            f"  price_diffs={len(report.price_diffs)}  "
            f"volume_diffs={len(report.volume_diffs)}\n"
            f"  reason: {reason}\n\n"
        )
    return log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trading_os.connectors.tiingo.bars_cli")
    ap.add_argument("--symbols", help="comma-separated tickers (default: all seeded)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the number of securities (for a quick pipeline test)")
    ap.add_argument("--swap", action="store_true",
                    help="validate EXISTING staging and swap it into live (no re-fetch)")
    ap.add_argument("--force", action="store_true",
                    help="swap despite FAILING validation (requires --force-reason). "
                         "The gate stays strict; this is an explicit, logged human override.")
    ap.add_argument("--force-reason", default=None,
                    help="why the failing validation is acceptable; recorded to the "
                         "swap-override log. Required with --force.")
    ap.add_argument("--sample-size", type=int, default=5000,
                    help="stage-E value-comparison sample size")
    args = ap.parse_args(argv)

    config = TiingoConfig()
    live = _silver_dir(config)
    staging = staging_dir(live)
    live_exists = live.exists() and any(live.glob("*.parquet"))

    # --swap: no fetch; validate what's already staged, then swap.
    if args.swap:
        if not (staging.exists() and any(staging.glob("*.parquet"))):
            print("[tiingo] no staging to swap; run a build first.", file=sys.stderr)
            return 1
        if live_exists:
            report = validate_rebuild(silver_glob(live), silver_glob(staging),
                                      sample_size=args.sample_size)
            print(report.summary())
            if not report.passed:
                if not args.force:
                    print("[tiingo] validation FAILED — refusing to swap. Live untouched.\n"
                          "         If the failure is understood and acceptable, re-run with\n"
                          "         --force --force-reason \"...\".",
                          file=sys.stderr)
                    return 1
                if not (args.force_reason or "").strip():
                    print("[tiingo] --force requires --force-reason \"...\" explaining why "
                          "the failing validation is acceptable.", file=sys.stderr)
                    return 1
                _record_override(live, report, args.force_reason.strip())
                print(f"[tiingo] OVERRIDE: swapping despite FAILED validation.\n"
                      f"         reason: {args.force_reason.strip()}")
        else:
            print("[tiingo] no live silver to compare; swapping staging in as initial silver.")
        backup = swap_staging_into_live(live)
        print(f"[tiingo] swapped staging into live; previous silver -> {backup}")
        return 0

    # default: build staging + validate + report (no swap)
    with psycopg.connect(settings.pg_conninfo()) as conn:
        if args.symbols:
            requested = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        else:
            requested = all_seeded_symbols(conn, args.limit)
        client = TiingoClient(config)
        build = build_staging(conn, config, client, requested)

    _print_build_report(build)

    if live_exists:
        report = validate_rebuild(silver_glob(live), silver_glob(staging),
                                  sample_size=args.sample_size)
        print("\n" + report.summary())
        print("\n[tiingo] Above, stage B = Alpaca sessions Tiingo is missing "
              "(investigate any; likely dot-class symbol mismatches). "
              "Stage E = Alpaca-vs-Tiingo value diffs on 2016+ overlap (informational).")
        print("[tiingo] Review, then run with --swap to make Tiingo silver canonical.")
    else:
        print("\n[tiingo] no live silver to validate against; run with --swap to "
              "install staging as the initial silver.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())