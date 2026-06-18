"""
FRED/ALFRED connector CLI.

Usage (from repo root, venv active, TRADING_OS_FRED_KEY set via .env or export):
    python -m trading_os.connectors.fred.cli              # all seeded series
    python -m trading_os.connectors.fred.cli --series UNRATE
    python -m trading_os.connectors.fred.cli --dry-run    # download+parse, no DB
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import psycopg

from trading_os.config import settings

from .client import FredClient
from .config import SERIES, SERIES_IDS, FredConfig
from .models import SeriesMeta
from .parser import parse_bronze
from .writer import MacroWriter


def _series_meta_from_payload(series_id: str, payload: dict) -> SeriesMeta:
    arr = payload.get("seriess") or payload.get("series") or []
    s = arr[0] if arr else {}
    return SeriesMeta(
        series_id=series_id,
        title=s.get("title", series_id),
        units=s.get("units"),
        frequency=s.get("frequency"),
        seasonal_adjustment=s.get("seasonal_adjustment"),
    )


def ingest_series(series_id: str, conn, config: FredConfig,
                  client: FredClient, dry_run: bool) -> dict:
    refs = client.fetch_observations(series_id)  # one or more bronze chunks
    obs = []
    for ref in refs:
        obs.extend(parse_bronze(ref))
    summary = {"series": series_id, "bronze_files": len(refs),
               "vintage_rows": len(obs)}

    if dry_run:
        # distinct observation dates vs total vintage rows shows revision depth
        distinct_dates = len({o.obs_date for o in obs})
        summary["distinct_obs_dates"] = distinct_dates
        summary["mode"] = "dry-run (no DB writes)"
        return summary

    writer = MacroWriter(conn)
    source_id = writer.ensure_source()
    meta = _series_meta_from_payload(series_id, client.fetch_series_meta(series_id))
    writer.upsert_series(meta, source_id)

    batch_id = writer.open_batch("macro.observation", datetime.now(timezone.utc),
                                 source_id, {"series_id": series_id, "bronze": ref.path})
    try:
        written = writer.write_observations(obs, batch_id)
        writer.close_batch(batch_id, "succeeded", rows_in=len(obs), rows_out=written)
        conn.commit()
        summary.update(written=written, batch_id=batch_id)
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        try:
            writer.close_batch(batch_id, "failed", rows_in=len(obs), rows_out=0, error=str(e))
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="FRED/ALFRED macro connector (V0).")
    p.add_argument("--series", help="single series id from the seeded list")
    p.add_argument("--dry-run", action="store_true",
                   help="download + parse, but write nothing to Postgres")
    args = p.parse_args(argv)

    if args.series:
        sid = args.series.upper()
        if sid not in SERIES_IDS:
            print(f"Refused: {sid} not in the seeded series list {SERIES_IDS}. "
                  f"Add it to config.SERIES (and record the decision) to ingest.",
                  file=sys.stderr)
            return 2
        targets = [sid]
    else:
        targets = list(SERIES_IDS)

    config = FredConfig()
    client = FredClient(config)  # validates the API key up front

    if args.dry_run:
        for sid in targets:
            try:
                s = ingest_series(sid, None, config, client, dry_run=True)
                print(f"[ok] {sid}: " + ", ".join(f"{k}={v}" for k, v in s.items() if k != "series"))
            except Exception as e:  # noqa: BLE001
                print(f"[FAIL] {sid}: {e}", file=sys.stderr)
        return 0

    with psycopg.connect(settings.pg_conninfo()) as conn:
        for sid in targets:
            try:
                s = ingest_series(sid, conn, config, client, dry_run=False)
                print(f"[ok] {sid}: " + ", ".join(f"{k}={v}" for k, v in s.items() if k != "series"))
            except Exception as e:  # noqa: BLE001
                print(f"[FAIL] {sid}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())