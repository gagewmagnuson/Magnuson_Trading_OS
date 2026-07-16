"""
Bars writer — Postgres-side responsibilities for the Alpaca connector (DEC-003):
  * the ALPACA data source row, and a meta.ingest_batch row for lineage
  * identity resolution (resolve-and-skip, DEC-017)

The Parquet write itself is NOT here: it lives in the shared, source-independent
bars.writer.write_bars_parquet (DEC-024), so Alpaca, Tiingo, and the silver
rebuild all write identical semantics through one primitive. This class no longer
touches DuckDB or knowledge_time.
"""
from __future__ import annotations

from datetime import datetime

import psycopg

from .config import AlpacaConfig


class BarsWriter:
    def __init__(self, conn: psycopg.Connection, config: AlpacaConfig):
        self.conn = conn
        self.config = config

    # ---- Postgres: source + batch lineage -------------------------------
    def ensure_source(self) -> int:
        row = self.conn.execute(
            "select source_id from ref.data_source where name = 'ALPACA'"
        ).fetchone()
        if row:
            return row[0]
        return self.conn.execute(
            """
            insert into ref.data_source
                (name, kind, is_redistributable, base_url, license_notes)
            values ('ALPACA', 'prices', false, 'https://data.alpaca.markets',
                    'Alpaca market data (Basic plan). Unadjusted OHLCV; redistribution prohibited per Alpaca terms.')
            returning source_id
            """
        ).fetchone()[0]

    def open_batch(self, source_id: int, knowledge_time: datetime, params: dict) -> int:
        return self.conn.execute(
            """
            insert into meta.ingest_batch
                (source_id, dataset, knowledge_time, params, code_version, status)
            values (%s, 'bars_eod', %s, %s, 'alpaca-v1', 'running')
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

    # ---- identity resolution (resolve-and-skip, DEC-017) ----------------
    def resolve_security_ids(self, symbols: list[str]) -> dict[str, int]:
        """Return {symbol: security_id} for symbols in the master; omit the rest."""
        out: dict[str, int] = {}
        for s in symbols:
            r = self.conn.execute(
                "select sec.resolve_ticker(%s, current_date)", (s,)
            ).fetchone()
            if r and r[0] is not None:
                out[s] = r[0]
        return out