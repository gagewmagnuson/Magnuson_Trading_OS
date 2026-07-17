"""
Silver rebuild for Alpaca EOD bars (DEC-024).

Repo path: src/trading_os/connectors/alpaca/rebuild.py
Run:  python -m trading_os.connectors.alpaca.rebuild [--swap] [--sample-size N]

Re-derives the entire Alpaca bars silver layer from IMMUTABLE BRONZE under the
DEC-024 knowledge_time rule (market-close availability, not ingest time). This is
what makes historical as_of queries PIT-correct and washes out the old
ingest-time-stamped bars.

Layer contract (do not blur these):
  * Bronze answers "what did we receive?" — every fetch, forever, untouched.
  * Silver answers "what is the canonical reconstructed history?" — exactly one
    row per (security_id, session_date), the LATEST bronze fetch winning (the most-
    corrected vendor value). Older vintages remain in bronze, not silver.
The rebuild reads ONLY bronze; it never reads or mutates existing silver. Existing
silver is only ever renamed aside to a backup during the swap. This preserves the
guarantee that silver is reproducible from bronze alone.

Flow: replay bronze in fetched_at order -> dedup latest-fetch-wins -> write staging
via the shared bars.writer -> five-stage validation vs live (bars.validation) ->
atomic swap ONLY on --swap and ONLY if validation passes. Build+validate and swap
are separate so a human reviews the report before the irreversible step.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

from trading_os.bars.models import Bar
from trading_os.bars.validation import validate_rebuild
from trading_os.bars.writer import write_bars_parquet
from trading_os.config import settings

from .config import AlpacaConfig
from .parser import parse_bars
from .writer import BarsWriter


# ---- pure replay + dedup (the correctness hinge; no DB, no files) -----------

def reassemble(doc: dict) -> dict[str, list[dict]]:
    """Merge one bronze doc's pages into a {symbol: [raw bar dict, ...]} map,
    exactly as the live client accumulates bars during a fetch."""
    merged: dict[str, list[dict]] = defaultdict(list)
    for page in doc.get("pages", []):
        for sym, bars in (page.get("bars") or {}).items():
            merged[sym].extend(bars)
    return merged


def replay_and_dedup(
    reassembled_in_order: list[dict[str, list[dict]]],
    sec_id_by_symbol: dict[str, int],
) -> list[Bar]:
    """Replay reassembled bronze maps IN fetched_at ORDER, keeping the latest
    fetch's value for each (security_id, session_date). Input MUST be ordered
    oldest-first; later entries overwrite earlier ones -> latest-fetch-wins."""
    latest: dict[tuple[int, date], Bar] = {}
    for m in reassembled_in_order:
        for b in parse_bars(m, sec_id_by_symbol):
            latest[(b.security_id, b.session_date)] = b
    return list(latest.values())


# ---- bronze loading --------------------------------------------------------

def load_bronze_docs(bronze_dir: Path) -> list[dict]:
    """Load every bronze doc, sorted ascending by fetched_at (the total order
    that makes latest-fetch-wins well-defined)."""
    docs = [json.loads(p.read_text()) for p in bronze_dir.glob("bars_eod_*.json")]
    docs.sort(key=lambda d: d.get("fetched_at", ""))
    return docs


# ---- staging paths + swap --------------------------------------------------

def _staging_dir(config: AlpacaConfig) -> Path:
    return config.silver_dir.parent / "bars_eod_staging"


def _glob(d: Path) -> str:
    return f"{d.as_posix()}/*.parquet"


def swap_staging_into_live(config: AlpacaConfig) -> Path:
    """Atomically-ish replace live silver with staging: rename live -> timestamped
    backup, then staging -> live. Rolls back the first rename if the second fails.
    (A ~microsecond window exists between the two renames; acceptable for a manual
    single-operator rebuild with no concurrent readers. Symlink-swap is the upgrade
    if that ever changes.)"""
    live = config.silver_dir
    staging = _staging_dir(config)
    if not staging.exists():
        raise RuntimeError("no staging directory to swap; run a build first")
    backup = live.parent / f"bars_eod_backup_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    live.rename(backup)
    try:
        staging.rename(live)
    except Exception:
        backup.rename(live)  # roll back so the lake is never left without live silver
        raise
    return backup


# ---- orchestration ---------------------------------------------------------

@dataclass
class BuildResult:
    bronze_docs: int
    distinct_symbols: int
    resolved_symbols: int
    deduped_rows: int
    rows_written: int
    skipped: int
    batch_id: int


def build_staging(conn: psycopg.Connection, config: AlpacaConfig) -> BuildResult:
    """Replay bronze -> dedup -> write a single staging Parquet. Never touches live
    silver. Records a meta.ingest_batch row for lineage (marked as a rebuild)."""
    staging = _staging_dir(config)
    # Fresh staging every run (rebuild is deterministic from bronze).
    if staging.exists():
        for f in staging.glob("*.parquet"):
            f.unlink()

    docs = load_bronze_docs(config.bronze_dir)
    reassembled = [reassemble(d) for d in docs]  # already in fetched_at order

    all_symbols: set[str] = set()
    for m in reassembled:
        all_symbols.update(m.keys())

    writer = BarsWriter(conn, config)
    sec_map = writer.resolve_security_ids(sorted(all_symbols))

    deduped = replay_and_dedup(reassembled, sec_map)

    source_id = writer.ensure_source()
    batch_id = writer.open_batch(
        source_id, datetime.now(timezone.utc),
        {"rebuild": True, "bronze_docs": len(docs),
         "distinct_symbols": len(all_symbols), "resolved_symbols": len(sec_map)},
    )
    try:
        result = write_bars_parquet(deduped, staging, "ALPACA", batch_id)
        writer.close_batch(batch_id, "succeeded",
                           rows_in=len(deduped), rows_out=result.rows_written)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        writer.close_batch(batch_id, "failed", rows_in=len(deduped), rows_out=0,
                           error=str(e))
        conn.commit()
        raise

    return BuildResult(
        bronze_docs=len(docs),
        distinct_symbols=len(all_symbols),
        resolved_symbols=len(sec_map),
        deduped_rows=len(deduped),
        rows_written=result.rows_written,
        skipped=len(result.skipped),
        batch_id=batch_id,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trading_os.connectors.alpaca.rebuild")
    ap.add_argument("--swap", action="store_true",
                    help="after a PASSING validation, swap staging into live silver")
    ap.add_argument("--sample-size", type=int, default=5000,
                    help="stage-E value-comparison sample size")
    args = ap.parse_args(argv)

    config = AlpacaConfig()
    staging = _staging_dir(config)

    with psycopg.connect(settings.pg_conninfo()) as conn:
        build = build_staging(conn, config)

    print(f"[rebuild] bronze docs={build.bronze_docs}  distinct symbols="
          f"{build.distinct_symbols}  resolved={build.resolved_symbols}")
    print(f"[rebuild] deduped rows={build.deduped_rows:,}  written="
          f"{build.rows_written:,}  skipped={build.skipped}  batch={build.batch_id}")

    report = validate_rebuild(
        _glob(config.silver_dir), _glob(staging), sample_size=args.sample_size
    )
    print(report.summary())

    if not report.passed:
        print("[rebuild] VALIDATION FAILED — staging left in place, live untouched.",
              file=sys.stderr)
        return 1

    if args.swap:
        backup = swap_staging_into_live(config)
        print(f"[rebuild] swapped staging into live; previous silver -> {backup}")
    else:
        print("[rebuild] dry-run OK: staging built and validated. "
              "Re-run with --swap to replace live silver.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())