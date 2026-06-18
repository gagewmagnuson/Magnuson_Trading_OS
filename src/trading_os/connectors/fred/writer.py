"""
Bitemporal writer for macro data. Upserts series metadata, then appends vintage
observations into macro.observation. Append-only: the schema trigger blocks
UPDATE/DELETE; revisions are new rows distinguished by vintage_date.

The unique(series_id, obs_date, vintage_date) constraint makes ingestion
idempotent: re-running inserts nothing already present.
"""
from __future__ import annotations

from datetime import datetime

import psycopg

from .models import SeriesMeta, VintageObs


class MacroWriter:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def ensure_source(self) -> int:
        """Ensure the FRED data source row exists; return its id."""
        row = self.conn.execute(
            "select source_id from ref.data_source where name = 'FRED'"
        ).fetchone()
        if row:
            return row[0]
        return self.conn.execute(
            """
            insert into ref.data_source (name, kind, is_redistributable, base_url, license_notes)
            values ('FRED', 'macro', true, 'https://api.stlouisfed.org/fred',
                    'US gov public domain (St. Louis Fed). ALFRED vintages used for point-in-time.')
            returning source_id
            """
        ).fetchone()[0]

    def upsert_series(self, meta: SeriesMeta, source_id: int) -> None:
        self.conn.execute(
            """
            insert into macro.series (series_id, title, units, frequency, seasonal_adj, source_id)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (series_id) do update
              set title = excluded.title, units = excluded.units,
                  frequency = excluded.frequency, seasonal_adj = excluded.seasonal_adj
            """,
            (meta.series_id, meta.title, meta.units, meta.frequency,
             meta.seasonal_adjustment, source_id),
        )

    # ---- batch lifecycle ----
    def open_batch(self, dataset: str, knowledge_time: datetime,
                   source_id: int, params: dict) -> int:
        return self.conn.execute(
            """
            insert into meta.ingest_batch
                (source_id, dataset, knowledge_time, params, code_version, status)
            values (%s, %s, %s, %s, 'fred-v0', 'running')
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

    def write_observations(self, obs: list[VintageObs], batch_id: int) -> int:
        """
        Append vintage observations. Idempotent via the unique constraint:
        on_conflict do nothing skips vintages already stored.
        Returns count actually inserted.
        """
        written = 0
        for o in obs:
            if o.obs_date is None or o.vintage_date is None:
                continue
            res = self.conn.execute(
                """
                insert into macro.observation
                    (series_id, obs_date, value, vintage_date, realtime_start,
                     realtime_end, batch_id)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (series_id, obs_date, vintage_date) do nothing
                """,
                (o.series_id, o.obs_date, o.value, o.vintage_date,
                 o.vintage_date, o.realtime_end, batch_id),
            )
            written += res.rowcount
        return written