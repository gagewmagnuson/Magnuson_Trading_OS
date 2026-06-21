"""
OpenFIGI security-master CLI. Enriches every sec.security that still lacks a
FIGI (figi IS NULL) with its real FIGI identity, chunked + paced to OpenFIGI's
limits (auto-selected by whether TRADING_OS_OPENFIGI_KEY is set).

Targets come from the DATABASE, not a hardcoded list: the universe layer creates
identities; OpenFIGI enriches whatever is still un-enriched. Idempotent — a
re-run only touches rows still missing a FIGI. Per DEC-017, enrichment does NOT
change sec.security.source_id (that records the creator); the OPENFIGI-sourced
ingest_batch is the enrichment provenance.

Usage (from repo root, venv active):
    python -m trading_os.connectors.openfigi.cli              # enrich all missing
    python -m trading_os.connectors.openfigi.cli --limit 20   # first N only (testing)
    python -m trading_os.connectors.openfigi.cli --dry-run    # fetch+parse, no DB writes
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone

import psycopg

from trading_os.config import settings

from .client import OpenFigiClient
from .config import OpenFigiConfig
from .parser import parse_identities
from .writer import SecurityMasterWriter


def _targets_needing_figi(conn, limit: int | None) -> list[str]:
    """Current TICKER of every security with no FIGI yet; ordered for stable runs."""
    rows = conn.execute(
        """
        select si.id_value
        from sec.security s
        join sec.security_identifier si
          on si.security_id = s.security_id
         and si.id_type = 'TICKER'
         and si.valid_from <= current_date
         and (si.valid_to is null or si.valid_to >= current_date)
        where s.figi is null
        order by si.id_value
        """
    ).fetchall()
    tickers = [r[0] for r in rows]
    return tickers[:limit] if limit else tickers


def run(conn, config: OpenFigiConfig, client: OpenFigiClient,
        dry_run: bool, limit: int | None) -> int:
    tickers = _targets_needing_figi(conn, limit)
    if not tickers:
        print("[openfigi] nothing to do: every security already has a FIGI.")
        return 0

    n_req = math.ceil(len(tickers) / config.max_jobs_per_request)
    est_min = (n_req * config.request_interval) / 60.0
    print(f"[openfigi] {len(tickers)} securities need a FIGI "
          f"(keyed={bool(config.api_key)}, jobs/req={config.max_jobs_per_request}, "
          f"{n_req} request(s), ~{est_min:.1f} min)")

    ref, response = client.map_tickers(tickers)
    identities = parse_identities(tickers, response)
    missing = [t for t in tickers if t not in identities]

    if dry_run:
        for t in tickers:
            ident = identities.get(t)
            if ident:
                print(f"[ok] {t}: figi={ident.figi}, name={ident.name}")
        print(f"[openfigi] dry-run: {len(identities)} mapped, "
              f"{len(missing)} no-mapping; bronze={ref.path}")
        return 0

    writer = SecurityMasterWriter(conn)
    source_id = writer.ensure_source()
    batch_id = writer.open_batch(
        source_id, datetime.now(timezone.utc),
        {"requested": len(tickers), "keyed": bool(config.api_key), "bronze": ref.path},
    )
    enriched = clashed = 0
    try:
        for t in tickers:
            ident = identities.get(t)
            if not ident:
                continue
            try:
                # Per-ticker savepoint: one FIGI clash or bad row is isolated
                # and counted, never rolling back the whole enrichment batch.
                with conn.transaction():
                    if writer.enrich(ident):
                        enriched += 1
            except Exception as e:  # noqa: BLE001
                clashed += 1
                print(f"[clash] {t}: {e}", file=sys.stderr)
        writer.close_batch(batch_id, "succeeded",
                           rows_in=len(identities), rows_out=enriched)
        conn.commit()
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

    print(f"[openfigi] enriched={enriched}, no-mapping={len(missing)}, "
          f"clashed={clashed}; batch_id={batch_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OpenFIGI security-master enrichment.")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch + parse identities, but write nothing to Postgres")
    p.add_argument("--limit", type=int, default=None,
                   help="enrich only the first N un-enriched securities (testing)")
    args = p.parse_args(argv)

    config = OpenFigiConfig()
    client = OpenFigiClient(config)
    with psycopg.connect(settings.pg_conninfo()) as conn:
        return run(conn, config, client, args.dry_run, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())