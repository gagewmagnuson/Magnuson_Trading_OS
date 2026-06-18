"""
OpenFIGI security-master CLI. Enriches the 5 placeholder securities with real
FIGI identities.

Usage (from repo root, venv active):
    python -m trading_os.connectors.openfigi.cli            # all 5 tickers
    python -m trading_os.connectors.openfigi.cli --dry-run  # fetch+parse, no DB
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import psycopg

from trading_os.config import settings

from .client import OpenFigiClient
from .config import TICKERS, OpenFigiConfig
from .parser import parse_identities
from .writer import SecurityMasterWriter


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OpenFIGI security master (V0).")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch + parse identities, but write nothing to Postgres")
    args = p.parse_args(argv)

    config = OpenFigiConfig()
    client = OpenFigiClient(config)

    ref, response = client.map_tickers(TICKERS)
    identities = parse_identities(TICKERS, response)

    missing = [t for t in TICKERS if t not in identities]
    for t in missing:
        print(f"[warn] no FIGI mapping returned for {t}", file=sys.stderr)

    if args.dry_run:
        for t in TICKERS:
            ident = identities.get(t)
            if ident:
                print(f"[ok] {t}: figi={ident.figi}, name={ident.name}, "
                      f"type={ident.security_type}, mode=dry-run")
        print(f"bronze={ref.path}")
        return 0

    with psycopg.connect(settings.pg_conninfo()) as conn:
        writer = SecurityMasterWriter(conn)
        source_id = writer.ensure_source()
        batch_id = writer.open_batch(source_id, datetime.now(timezone.utc),
                                     {"tickers": TICKERS, "bronze": ref.path})
        updated = 0
        try:
            for t in TICKERS:
                ident = identities.get(t)
                if not ident:
                    continue
                if writer.enrich(ident, source_id):
                    updated += 1
                    print(f"[ok] {t}: figi={ident.figi}, name={ident.name}")
            writer.close_batch(batch_id, "succeeded",
                               rows_in=len(identities), rows_out=updated)
            conn.commit()
            print(f"enriched {updated} securities; batch_id={batch_id}")
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            try:
                writer.close_batch(batch_id, "failed", rows_in=len(identities),
                                   rows_out=0, error=str(e))
                conn.commit()
            except Exception:
                conn.rollback()
            print(f"[FAIL] {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())