"""
Bars writer. Two responsibilities, two stores (DEC-003):
  * Postgres: the ALPACA data source, and a meta.ingest_batch row for lineage.
  * Parquet : the unadjusted bars themselves, one file per ingest batch.

Identity resolution is resolve-and-skip (DEC-017): this connector never creates
a sec.security. The Parquet write uses DuckDB (already a dependency) — no extra
library — mirroring how the engine reads the lake.
"""
from __future__ import annotations

from datetime import datetime

import duckdb
import psycopg

from .config import AlpacaConfig
from .models import Bar

_BARS_DDL = """
create table bars (
    security_id     BIGINT,
    symbol          VARCHAR,
    session_date    DATE,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    close           DOUBLE,
    volume          BIGINT,
    trade_count     BIGINT,
    vwap            DOUBLE,
    knowledge_time  TIMESTAMPTZ,
    ingest_batch_id BIGINT,
    source          VARCHAR
)
"""


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

    # ---- Parquet: the bars themselves -----------------------------------
    def write_bars_parquet(self, bars: list[Bar], knowledge_time: datetime,
                           batch_id: int) -> int:
        """
        Write all bars for this batch to one append-only Parquet file. Writes to
        a temp name and atomically renames, so a partial file never appears in
        the lake glob. Returns rows written.
        """
        if not bars:
            return 0
        self.config.silver_dir.mkdir(parents=True, exist_ok=True)
        out_file = self.config.silver_dir / f"bars_eod_batch_{batch_id}.parquet"
        tmp_file = out_file.with_suffix(".parquet.tmp")

        con = duckdb.connect()
        try:
            con.execute(_BARS_DDL)
            rows = [
                (b.security_id, b.symbol, b.session_date, b.open, b.high, b.low,
                 b.close, b.volume, b.trade_count, b.vwap,
                 knowledge_time, batch_id, "ALPACA")
                for b in bars
            ]
            con.executemany(
                "insert into bars values (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
            con.execute(f"copy bars to '{tmp_file.as_posix()}' (FORMAT PARQUET)")
        finally:
            con.close()
        tmp_file.replace(out_file)
        return len(bars)