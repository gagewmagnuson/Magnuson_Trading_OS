"""
Writer for trading-calendar reference data.

ref.exchange and ref.trading_session are MUTABLE reference data, not append-only
fact tables (no knowledge_time; excluded from the no-mutation triggers). The
table is a cache of the library output, so the writer UPSERTS — re-running
refreshes/extends without duplication. This is the one place the writer pattern
differs from the append-only fact writers (macro, fundamentals). Every run still
records a meta.ingest_batch row, including the library version in params.
"""
from __future__ import annotations

from datetime import datetime

import psycopg

from .models import ExchangeMeta, SessionRow


class CalendarWriter:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def ensure_source(self) -> int:
        row = self.conn.execute(
            "select source_id from ref.data_source where name = 'EXCHANGE_CALENDARS'"
        ).fetchone()
        if row:
            return row[0]
        return self.conn.execute(
            """
            insert into ref.data_source (name, kind, is_redistributable, base_url, license_notes)
            values ('EXCHANGE_CALENDARS', 'reference', true,
                    'https://github.com/gerrymanoim/exchange_calendars',
                    'OSS (Apache-2.0). Exchange sessions are public facts; redistributable.')
            returning source_id
            """
        ).fetchone()[0]

    def ensure_exchange(self, meta: ExchangeMeta) -> int:
        return self.conn.execute(
            """
            insert into ref.exchange (mic, name, country, timezone)
            values (%s, %s, %s, %s)
            on conflict (mic) do update
              set name = excluded.name,
                  country = excluded.country,
                  timezone = excluded.timezone
            returning exchange_id
            """,
            (meta.mic, meta.name, meta.country, meta.timezone),
        ).fetchone()[0]

    def open_batch(self, dataset: str, knowledge_time: datetime,
                   source_id: int, params: dict) -> int:
        return self.conn.execute(
            """
            insert into meta.ingest_batch
                (source_id, dataset, knowledge_time, params, code_version, status)
            values (%s, %s, %s, %s, 'calendars-v0', 'running')
            returning batch_id
            """,
            (source_id, dataset, knowledge_time, psycopg.types.json.Json(params)),
        ).fetchone()[0]

    def close_batch(self, batch_id: int, status: str, rows_in: int, rows_out: int,
                    error: str | None = None) -> None:
        self.conn.execute(
            """
            update meta.ingest_batch set status=%s, finished_at=now(),
                   rows_in=%s, rows_out=%s, error=%s
             where batch_id=%s
            """,
            (status, rows_in, rows_out, error, batch_id),
        )

    def upsert_sessions(self, exchange_id: int, sessions: list[SessionRow]) -> int:
        """
        Idempotent upsert on (exchange_id, session_date). A re-run refreshes
        changed sessions and inserts new ones; rows are never duplicated
        (idempotency = convergent state on the PK). Returns rows affected.
        """
        affected = 0
        for s in sessions:
            res = self.conn.execute(
                """
                insert into ref.trading_session
                    (exchange_id, session_date, open_utc, close_utc, is_half_day)
                values (%s, %s, %s, %s, %s)
                on conflict (exchange_id, session_date) do update
                  set open_utc = excluded.open_utc,
                      close_utc = excluded.close_utc,
                      is_half_day = excluded.is_half_day
                """,
                (exchange_id, s.session_date, s.open_utc, s.close_utc, s.is_half_day),
            )
            affected += res.rowcount
        return affected