"""
Universe seeder entrypoint.

Loads every registered coverage source, resolves CIKs from SEC
company_tickers.json, then creates-or-reconciles a sec.security identity per
ticker. Reports per-source coverage, CIK resolution, and create/reconcile counts.

Run:
    PYTHONPATH=src python -m trading_os.universe.cli --dry-run   # no DB writes
    PYTHONPATH=src python -m trading_os.universe.cli             # seed

Repo path: src/trading_os/universe/cli.py
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import psycopg

from trading_os.config import settings

from . import config as cfg
from .cik import CikResolver
from .coverage import load_coverage
from .writer import UniverseWriter


def run(conn, config, dry_run: bool) -> int:
    entries, reports = load_coverage(config)

    if not reports:
        print("[universe] no coverage sources registered. Add rows to "
              "manifests/registry.csv.", file=sys.stderr)
        return 1
    for r in reports:
        print(f"[universe] source {r.name}: "
              f"{'present' if r.present else 'MISSING'} ({r.count})")
    if not entries:
        print("[universe] registry has sources but no manifests yielded tickers. "
              "Generate the manifests (build_manifest) before seeding.", file=sys.stderr)
        return 1

    by_type: dict[str, int] = {}
    for e in entries:
        by_type[e.security_type] = by_type.get(e.security_type, 0) + 1
    print(f"[universe] coverage: {len(entries)} "
          f"({', '.join(f'{k}={v}' for k, v in sorted(by_type.items()))})")

    resolver = CikResolver(config)
    try:
        resolver.load()
    except Exception as exc:  # network / SEC fetch failure -> stop, do not seed
        print(f"[universe] FAILED to fetch company_tickers.json: {exc}", file=sys.stderr)
        return 1
    missing = [e.ticker for e in entries if resolver.resolve(e.ticker) is None]
    print(f"[universe] CIK resolved: {len(entries) - len(missing)}/{len(entries)} "
          f"(missing {len(missing)})"
          + (f" e.g. {', '.join(missing[:8])}" if missing else ""))

    if dry_run:
        print("[universe] dry-run: no DB writes.")
        return 0

    writer = UniverseWriter(conn, config)
    source_id = writer.ensure_source()
    batch_id = writer.open_batch(
        datetime.now(timezone.utc), source_id,
        {"coverage": len(entries), "by_type": by_type,
         "sources": {r.name: r.count for r in reports}},
    )
    created = reconciled = 0
    try:
        for e in entries:
            if writer.create_or_reconcile(e, resolver.resolve(e.ticker), source_id, batch_id):
                created += 1
            else:
                reconciled += 1
        writer.close_batch(batch_id, "succeeded",
                           rows_in=len(entries), rows_out=created + reconciled)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        try:
            writer.close_batch(batch_id, "failed", rows_in=len(entries),
                               rows_out=0, error=str(exc))
            conn.commit()
        except Exception:
            conn.rollback()
        raise

    print(f"[universe] DB: created={created}, reconciled={reconciled} (batch {batch_id})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Universe/security-master seeder (identities + CIK)."
    )
    p.add_argument("--dry-run", action="store_true",
                   help="load coverage + resolve CIK, no DB writes")
    args = p.parse_args(argv)
    config = cfg.UniverseConfig()

    if args.dry_run:
        return run(None, config, dry_run=True)
    with psycopg.connect(settings.pg_conninfo()) as conn:
        return run(conn, config, dry_run=False)


if __name__ == "__main__":
    raise SystemExit(main())