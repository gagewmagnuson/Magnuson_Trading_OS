"""
Tiingo corporate-actions writer. Mirrors the Alpaca BarsWriter's two-store
pattern (Postgres source + meta.ingest_batch lineage), but the actual action
write goes through the shared canonical primitive corp.action_write (one
implementation for every source). This is the CANONICAL corporate-action source
(source_id = TIINGO), distinct from the BOOTSTRAP loader's rows.

knowledge_time = ingest fetch time (DEC-018): the OS records when IT first knew
an action, not the market's announcement time. The ex_date remains the price-
effect anchor used by the adjustment engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import psycopg

from trading_os.corp.action_write import write_actions
from trading_os.engine.adjust import Action

from .config import TiingoConfig


@dataclass
class BatchStats:
    """Operational metrics recorded into meta.ingest_batch.params."""
    symbols_processed: int = 0
    symbols_failed: int = 0
    api_calls: int = 0
    inserted: int = 0
    skipped_exact: int = 0
    conflicts: int = 0
    elapsed_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def as_params(self, extra: dict) -> dict:
        d = {
            "symbols_processed": self.symbols_processed,
            "symbols_failed": self.symbols_failed,
            "api_calls": self.api_calls,
            "inserted": self.inserted,
            "skipped_exact": self.skipped_exact,
            "conflicts": self.conflicts,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            # Persist warnings so odd split ratios / conflicts are answerable
            # later without re-running (capped to keep the row reasonable).
            "warnings": self.warnings[:200],
            "failures": self.failures[:50],
        }
        d.update(extra)
        return d


class ActionsWriter:
    def __init__(self, conn: psycopg.Connection, config: TiingoConfig):
        self.conn = conn
        self.config = config

    # ---- Postgres: canonical source + batch lineage --------------------
    def ensure_source(self) -> int:
        row = self.conn.execute(
            "select source_id from ref.data_source where name = 'TIINGO'"
        ).fetchone()
        if row:
            return row[0]
        return self.conn.execute(
            """
            insert into ref.data_source
                (name, kind, is_redistributable, base_url, license_notes)
            values ('TIINGO', 'corporate_actions', false, 'https://api.tiingo.com',
                    'Tiingo daily feed: splits + cash dividends (CRSP-following). Redistribution prohibited per Tiingo terms.')
            returning source_id
            """
        ).fetchone()[0]

    def open_batch(self, source_id: int, knowledge_time: datetime,
                   params: dict) -> int:
        return self.conn.execute(
            """
            insert into meta.ingest_batch
                (source_id, dataset, knowledge_time, params, code_version, status)
            values (%s, 'corporate_actions', %s, %s, 'tiingo-actions-v1', 'running')
            returning batch_id
            """,
            (source_id, knowledge_time, psycopg.types.json.Json(params)),
        ).fetchone()[0]

    def close_batch(self, batch_id: int, status: str, rows_in: int, rows_out: int,
                    params: dict, error: str | None = None) -> None:
        self.conn.execute(
            """
            update meta.ingest_batch set status=%s, finished_at=now(),
                   rows_in=%s, rows_out=%s, params=%s, error=%s
            where batch_id=%s
            """,
            (status, rows_in, rows_out, psycopg.types.json.Json(params),
             error, batch_id),
        )

    # ---- identity resolution (resolve-and-skip, DEC-017) ---------------
    def resolve_security_ids(self, symbols: list[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in symbols:
            r = self.conn.execute(
                "select sec.resolve_ticker(%s, current_date)", (s,)
            ).fetchone()
            if r and r[0] is not None:
                out[s] = r[0]
        return out

    # ---- canonical action write (via shared primitive) -----------------
    def write_symbol_actions(self, actions: list[Action], knowledge_time: datetime,
                             label: str):
        """Write one symbol's actions under source TIINGO at ingest knowledge_time.
        Returns the shared WriteResult (inserted/skipped_exact/conflicts/warnings).
        Does NOT commit — the CLI controls the transaction boundary per symbol."""
        source_id = self.ensure_source()
        return write_actions(self.conn, actions, source_id, knowledge_time, label)