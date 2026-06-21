"""
Security-master writer. Enriches EXISTING sec.security rows in place with real
FIGI identity. sec.security is reference data (no append-only trigger), so
UPDATE is the correct operation — this corrects identity metadata, it does not
rewrite a historical fact. security_id values stay stable, so every existing
fundamental_fact stays correctly linked.

Also ensures the OpenFIGI data source exists and writes a batch row for lineage.
"""
from __future__ import annotations

from datetime import datetime

import psycopg

from .models import FigiIdentity


class SecurityMasterWriter:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def ensure_source(self) -> int:
        row = self.conn.execute(
            "select source_id from ref.data_source where name = 'OPENFIGI'"
        ).fetchone()
        if row:
            return row[0]
        return self.conn.execute(
            """
            insert into ref.data_source (name, kind, is_redistributable, base_url, license_notes)
            values ('OPENFIGI', 'reference', false, 'https://api.openfigi.com',
                    'OpenFIGI mapping API. FIGI identifiers are open; usage subject to OpenFIGI terms.')
            returning source_id
            """
        ).fetchone()[0]

    def open_batch(self, source_id: int, knowledge_time: datetime, params: dict) -> int:
        return self.conn.execute(
            """
            insert into meta.ingest_batch
                (source_id, dataset, knowledge_time, params, code_version, status)
            values (%s, 'sec.security', %s, %s, 'openfigi-v0', 'running')
            returning batch_id
            """,
            (source_id, knowledge_time, psycopg.types.json.Json(params)),
        ).fetchone()[0]

    def close_batch(self, batch_id: int, status: str, rows_in: int, rows_out: int,
                    error: str | None = None) -> None:
        self.conn.execute(
            """
            update meta.ingest_batch set status=%s, finished_at=now(),
                   rows_in=%s, rows_out=%s, error=%s where batch_id=%s
            """,
            (status, rows_in, rows_out, error, batch_id),
        )

    def enrich(self, identity: FigiIdentity) -> bool:
        """
        Enrich the sec.security row for identity.ticker, resolved via its
        current TICKER identifier. Returns True if a row was updated.

        Only fills the FIGI anchor, composite_figi, a real description, and
        country/currency defaults. Does NOT touch security_id or the ticker
        identifier's effective dates (real listing dates are a later refinement).
        """
        sec_id = self.conn.execute(
            "select sec.resolve_ticker(%s, current_date)", (identity.ticker,)
        ).fetchone()
        sec_id = sec_id[0] if sec_id else None
        if sec_id is None:
            return False

        # Guard the unique(figi) constraint: if this FIGI is already on a
        # DIFFERENT security, do not duplicate it — flag by skipping.
        clash = self.conn.execute(
            "select security_id from sec.security where figi = %s and security_id <> %s",
            (identity.figi, sec_id),
        ).fetchone()
        if clash:
            raise RuntimeError(
                f"FIGI {identity.figi} for {identity.ticker} already assigned to "
                f"security_id {clash[0]}; refusing to duplicate."
            )

        # DEC-017: source_id is the CREATOR and is left untouched on enrichment.
        # Enrichment provenance lives in the OPENFIGI ingest_batch, not here.
        self.conn.execute(
            """
            update sec.security
               set figi = %s,
                   composite_figi = %s,
                   description = %s,
                   country = coalesce(country, 'US')
             where security_id = %s
            """,
            (identity.figi, identity.share_class_figi,
             identity.name or identity.ticker, sec_id),
        )
        return True