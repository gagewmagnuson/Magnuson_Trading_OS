"""
Trading-calendar connector CLI.

Populates ref.exchange + ref.trading_session for the pinned XNYS window.
Idempotent: safe to re-run; extends the window when END is bumped.

    python -m trading_os.connectors.calendars.cli            # populate XNYS
    python -m trading_os.connectors.calendars.cli --dry-run  # build, no DB writes
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import psycopg

from trading_os.config import settings

from .client import CalendarClient
from .config import CalendarsConfig
from .writer import CalendarWriter


def populate(conn, config: CalendarsConfig, client: CalendarClient,
             dry_run: bool) -> dict:
    sessions = client.sessions()
    summary = {
        "mic": config.mic, "calendar": config.calendar_code,
        "start": config.start.isoformat(), "end": config.end.isoformat(),
        "sessions": len(sessions), "lib_version": client.library_version(),
    }
    if dry_run:
        summary["half_days"] = sum(1 for s in sessions if s.is_half_day)
        summary["mode"] = "dry-run (no DB writes)"
        return summary

    writer = CalendarWriter(conn)
    source_id = writer.ensure_source()
    exchange_id = writer.ensure_exchange(client.exchange_meta())
    params = {
        "mic": config.mic, "calendar_code": config.calendar_code,
        "start": config.start.isoformat(), "end": config.end.isoformat(),
        "exchange_calendars_version": client.library_version(),
    }
    batch_id = writer.open_batch("ref.trading_session",
                                 datetime.now(timezone.utc), source_id, params)
    try:
        affected = writer.upsert_sessions(exchange_id, sessions)
        writer.close_batch(batch_id, "succeeded",
                           rows_in=len(sessions), rows_out=affected)
        conn.commit()
        summary.update(exchange_id=exchange_id, rows_affected=affected,
                       batch_id=batch_id)
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        try:
            writer.close_batch(batch_id, "failed",
                               rows_in=len(sessions), rows_out=0, error=str(e))
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Trading-calendar connector (V0, XNYS).")
    p.add_argument("--dry-run", action="store_true",
                   help="build sessions from the library, write nothing to Postgres")
    args = p.parse_args(argv)

    config = CalendarsConfig()
    client = CalendarClient(config)

    if args.dry_run:
        s = populate(None, config, client, dry_run=True)
        print("[ok] " + ", ".join(f"{k}={v}" for k, v in s.items()))
        return 0

    with psycopg.connect(settings.pg_conninfo()) as conn:
        try:
            s = populate(conn, config, client, dry_run=False)
            print("[ok] " + ", ".join(f"{k}={v}" for k, v in s.items()))
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {config.mic}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())