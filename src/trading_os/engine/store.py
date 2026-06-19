"""
DuckDB analytical engine — the V0 cross-store, point-in-time capstone.

Repo path: src/trading_os/engine/store.py

DuckDB reads the Parquet lake in place and ATTACHes PostgreSQL READ-ONLY, so a
single query is point-in-time across BOTH stores with no data duplication
(SCHEMA.md §4/§5, DEC-003). Postgres remains the system of record.

================================ IMPORTANT =================================
The Parquet export below is a ONE-OFF V0 ARCHITECTURAL PROOF, not an ETL
pattern. It is a sanctioned, transient exception to DEC-003's no-duplication
rule: a derived, regenerable, NON-authoritative snapshot used only to prove
that the architecture composes across the store boundary. DO NOT generalize it
into a standing Postgres -> Parquet pipeline. Real Parquet data (price bars)
arrives in V1 via Alpaca; that is the first and only intended permanent Parquet
producer. Postgres stays the system of record for facts.
===========================================================================

Implementation note: all Postgres access here uses attached-TABLE reads
(`pg.<schema>.<table>`) with the point-in-time logic expressed in DuckDB SQL
(the same DISTINCT-ON / latest-vintage pattern the Postgres *_asof() helpers
use). This deliberately avoids invoking Postgres set-returning functions
through the attach, whose support is version-dependent — keeping the proof on
the most robust, well-supported subset of the DuckDB postgres extension. The
Postgres-native *_asof() functions are exercised separately by the test suite,
which proves the two paths agree (test_parquet_path_equals_postgres_asof).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb

from trading_os.config import settings

from . import config as cfg


class DuckDBStore:
    def __init__(self, config: cfg.EngineConfig | None = None):
        self.config = config or cfg.EngineConfig()
        self.con: duckdb.DuckDBPyConnection | None = None

    # ---- lifecycle ----
    def connect(self) -> None:
        """Open an in-memory DuckDB, load the postgres extension, and attach
        Postgres READ-ONLY. INSTALL downloads the extension once (needs internet
        on the first run only)."""
        con = duckdb.connect()
        con.execute("INSTALL postgres")
        con.execute("LOAD postgres")
        attach = settings.pg_conninfo()
        con.execute(f"ATTACH '{attach}' AS pg (TYPE postgres, READ_ONLY)")
        self.con = con

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None

    def __enter__(self) -> "DuckDBStore":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- export (the transient proof artifact) ----
    def export_macro_observations(self) -> int:
        """
        Snapshot the FULL bitemporal macro.observation (ALL vintages) from
        Postgres to a derived Parquet file. Returns the row count written.

        Regenerable and non-authoritative (see module docstring). Overwrites any
        prior snapshot so the proof is deterministic. Uses a DuckDB COPY that
        reads the attached Postgres table directly — no extra dependency.
        """
        assert self.con is not None, "call connect() first"
        out_dir: Path = self.config.macro_silver_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "macro_observation.parquet"
        self.con.execute(
            "COPY (SELECT series_id, obs_date, value, vintage_date "
            "FROM pg.macro.observation) "
            f"TO '{out_file.as_posix()}' (FORMAT PARQUET)"
        )
        row = self.con.execute(
            f"SELECT count(*) FROM read_parquet('{self._macro_glob()}')"
        ).fetchone()
        return int(row[0]) if row else 0

    # ---- point-in-time read from the Parquet side ----
    def macro_value_asof(self, series_id: str, obs_date: date, as_of: date):
        """Value of (series_id, obs_date) as KNOWN on as_of, computed from the
        Parquet snapshot: latest vintage_date <= as_of. Mirrors
        macro.observations_asof, but through Parquet + DuckDB."""
        assert self.con is not None, "call connect() first"
        row = self.con.execute(
            f"""
            SELECT value FROM (
                SELECT value,
                       row_number() OVER (ORDER BY vintage_date DESC) AS rn
                FROM read_parquet('{self._macro_glob()}')
                WHERE series_id = ? AND obs_date = ? AND vintage_date <= ?
            ) WHERE rn = 1
            """,
            [series_id, obs_date, as_of],
        ).fetchone()
        return row[0] if row else None

    # ---- the capstone: ONE PIT query spanning Parquet + Postgres ----
    def capstone_world_state(self, as_of: date) -> list[tuple]:
        """
        A single point-in-time query, as-of one date, composing:
          * macro context (10Y Treasury yield) from the PARQUET snapshot, PIT,
          * the latest-known total_assets per security from POSTGRES (real
            EDGAR), PIT, joined to the security master for identity.
        One row per security:
          (security_id, figi, description, total_assets, period_end_date,
           context_10y_yield).
        """
        assert self.con is not None, "call connect() first"
        as_of_d = as_of.isoformat()
        as_of_ts = f"{as_of_d} 23:59:59+00"
        glob = self._macro_glob()
        sql = f"""
        WITH macro_pit AS (   -- PARQUET side, PIT (latest vintage <= as_of)
            SELECT series_id, obs_date, value FROM (
                SELECT series_id, obs_date, value,
                       row_number() OVER (PARTITION BY series_id, obs_date
                                          ORDER BY vintage_date DESC) AS rn
                FROM read_parquet('{glob}')
                WHERE vintage_date <= DATE '{as_of_d}'
            ) WHERE rn = 1
        ),
        context AS (          -- contemporaneous 10Y yield, as-of
            SELECT value AS dgs10 FROM macro_pit
            WHERE series_id = '{cfg.CONTEXT_SERIES}'
            ORDER BY obs_date DESC LIMIT 1
        ),
        fundamentals AS (     -- POSTGRES side (attached tables), PIT total_assets
            SELECT security_id, figi, description, total_assets, period_end_date
            FROM (
                SELECT s.security_id, s.figi, s.description,
                       ff.value AS total_assets, ff.period_end_date,
                       row_number() OVER (PARTITION BY ff.security_id
                                          ORDER BY ff.period_end_date DESC,
                                                   ff.filed_at DESC) AS rn
                FROM pg.fund.fundamental_fact ff
                JOIN pg.fund.concept c ON c.concept_id  = ff.concept_id
                JOIN pg.sec.security s ON s.security_id = ff.security_id
                WHERE c.canonical_name = 'total_assets'
                  AND ff.period_start IS NULL            -- balance-sheet instant
                  AND ff.filed_at <= TIMESTAMPTZ '{as_of_ts}'
            ) WHERE rn = 1
        )
        SELECT f.security_id, f.figi, f.description, f.total_assets,
               f.period_end_date, x.dgs10 AS context_10y_yield
        FROM fundamentals f CROSS JOIN context x
        ORDER BY f.security_id
        """
        return self.con.execute(sql).fetchall()

    # ---- helpers ----
    def _macro_glob(self) -> str:
        return (self.config.macro_silver_dir / "*.parquet").as_posix()