"""
Postgres writer for the universe layer. Owns identity creation.

Mirrors the EDGAR security-creation shape exactly (TICKER identifier at
valid_from '1900-01-01', figi left NULL) and adds the CIK column. The one
behavioural difference from a connector: on an already-existing ticker it
RECONCILES (sets CIK if we have one, never clobbering a value with NULL) rather
than touching the identity. Idempotent: re-running creates nothing new.

Repo path: src/trading_os/universe/writer.py
"""
from __future__ import annotations

from datetime import datetime

import psycopg

from . import config as cfg
from .models import CoverageEntry


class UniverseWriter:
    def __init__(self, conn: psycopg.Connection, config):
        self.conn = conn
        self.config = config

    # ---- source + batch lifecycle ---------------------------------------
    def ensure_source(self) -> int:
        """Get (or create) the 'UNIVERSE' ref.data_source row."""
        row = self.conn.execute(
            "select source_id from ref.data_source where name = %s",
            (cfg.UNIVERSE_SOURCE_NAME,),
        ).fetchone()
        if row:
            return row[0]
        return self.conn.execute(
            """
            insert into ref.data_source
                (name, kind, is_redistributable, base_url, license_notes)
            values (%s, 'reference', false, null,
                    'Universe/security-master layer: sole creator of security identities (DEC-017). Coverage seeded from committed manifests; the SPY-derived equity list is a large-cap SEED universe, NOT authoritative S&P 500 membership.')
            returning source_id
            """,
            (cfg.UNIVERSE_SOURCE_NAME,),
        ).fetchone()[0]

    def open_batch(self, knowledge_time: datetime, source_id: int, params: dict) -> int:
        return self.conn.execute(
            """
            insert into meta.ingest_batch
                (source_id, dataset, knowledge_time, params, code_version, status)
            values (%s, 'sec.security', %s, %s, 'universe-v1', 'running')
            returning batch_id
            """,
            (source_id, knowledge_time, psycopg.types.json.Json(params)),
        ).fetchone()[0]

    def close_batch(self, batch_id: int, status: str, rows_in: int, rows_out: int,
                    error: str | None = None) -> None:
        self.conn.execute(
            """
            update meta.ingest_batch
               set status = %s, finished_at = now(),
                   rows_in = %s, rows_out = %s, error = %s
             where batch_id = %s
            """,
            (status, rows_in, rows_out, error, batch_id),
        )

    # ---- identity create / reconcile ------------------------------------
    def create_or_reconcile(self, entry: CoverageEntry, cik: str | None,
                            source_id: int, batch_id: int) -> bool:
        """
        Return True if a new identity was created, False if an existing one was
        reconciled. Reconcile only fills CIK (coalesce keeps any existing value);
        it never alters security_type, description, source_id, or the identifier.
        """
        existing = self.conn.execute(
            "select sec.resolve_ticker(%s, current_date)",
            (entry.ticker,),
        ).fetchone()
        if existing and existing[0] is not None:
            self.conn.execute(
                "update sec.security set cik = coalesce(%s, cik) where security_id = %s",
                (cik, existing[0]),
            )
            return False

        sec_id = self.conn.execute(
            """
            insert into sec.security (security_type, description, source_id, cik)
            values (%s, %s, %s, %s)
            returning security_id
            """,
            (entry.security_type,
             f"{entry.ticker} (seeded by universe layer)",
             source_id, cik),
        ).fetchone()[0]

        self.conn.execute(
            """
            insert into sec.security_identifier
                (security_id, id_type, id_value, valid_from, knowledge_time, batch_id)
            values (%s, 'TICKER', %s, date '1900-01-01', now(), %s)
            """,
            (sec_id, entry.ticker, batch_id),
        )
        return True