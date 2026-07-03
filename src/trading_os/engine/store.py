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

from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from trading_os.config import settings

from . import config as cfg


class DuckDBStore:
    def __init__(self, config: cfg.EngineConfig | None = None):
        self.config = config or cfg.EngineConfig()
        self.con: duckdb.DuckDBPyConnection | None = None

    # ---- lifecycle ----
    def connect(self, attach_postgres: bool = True) -> None:
        """Open an in-memory DuckDB. When attach_postgres is True (default), load
        the postgres extension and ATTACH Postgres READ-ONLY for cross-store
        queries. Pure-Parquet reads (e.g. bars_eod_asof) pass False to skip the
        attach, so they need no Postgres at all. INSTALL downloads the extension
        once (needs internet on the first run only)."""
        con = duckdb.connect()
        if attach_postgres:
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

    # ---- point-in-time read: EOD bars (generic, vendor-agnostic) ----
    def bars_eod_asof(self, as_of, security_ids=None, start=None, end=None, adjustment=None):
        """
        EOD bars as KNOWN on `as_of`, from the silver Parquet lake: for each
        (security_id, session_date), the row with the latest knowledge_time
        <= as_of. No lookahead; a revision appears only from its knowledge_time
        onward. Generic over any bars producer — knows only the bars_eod schema.

        adjustment: None -> raw stored bars (default, unchanged behaviour).
                    "split" -> split-adjusted prices (+ volume), continuous.
                    "total_return" -> split + cash-dividend adjusted prices.
        Adjustment is computed ON READ (DEC-004): raw storage is never mutated.
        Only corporate actions KNOWN by as_of (ex_date <= as_of AND
        knowledge_time <= as_of) are applied — the same as_of that filters bars,
        so the adjustment itself is lookahead-free. Requires the Postgres attach
        (connect(attach_postgres=True)).

        Returns rows:
        (security_id, symbol, session_date, open, high, low, close,
        volume, trade_count, vwap, knowledge_time, source)
        """
        assert self.con is not None, "call connect() first"
        import glob as _glob
        if not _glob.glob(self._bars_glob()):
            return []
        if adjustment is None:
            return self._bars_eod_asof_raw(as_of, security_ids, start, end)

        from .adjust import apply_factors_to_bars, compute_adjustment_factors
        # Adjust over each security's FULL history (so an action's day-before-ex
        # reference close and every later factor are present), then slice.
        full = self._bars_eod_asof_raw(as_of, security_ids, None, None)
        if not full:
            return []
        sec_ids = sorted({b[0] for b in full})
        actions = self._fetch_actions(sec_ids, as_of)
        closes: dict = {}
        sessions: dict = {}
        for b in full:
            closes.setdefault(b[0], {})[b[2]] = b[6]      # close
            sessions.setdefault(b[0], []).append(b[2])    # already ordered by query
        factors = compute_adjustment_factors(actions, closes, sessions,
                                            mode=adjustment)
        adjusted = apply_factors_to_bars(full, factors)
        if start is not None:
            adjusted = [b for b in adjusted if b[2] >= start]
        if end is not None:
            adjusted = [b for b in adjusted if b[2] <= end]
        return adjusted

    def _bars_eod_asof_raw(self, as_of, security_ids=None, start=None, end=None):
        """The raw PIT bar query (latest knowledge_time <= as_of per
        (security_id, session_date)). Pure Parquet; no Postgres needed."""
        where = ["knowledge_time <= ?"]
        args: list = [self._as_of_ts(as_of)]
        if security_ids:
            where.append(f"security_id in ({','.join('?' for _ in security_ids)})")
            args.extend(security_ids)
        if start is not None:
            where.append("session_date >= ?"); args.append(start)
        if end is not None:
            where.append("session_date <= ?"); args.append(end)
        sql = f"""
        SELECT security_id, symbol, session_date, open, high, low, close,
            volume, trade_count, vwap, knowledge_time, source
        FROM (
            SELECT *,
                row_number() OVER (PARTITION BY security_id, session_date
                                    ORDER BY knowledge_time DESC) AS rn
            FROM read_parquet('{self._bars_glob()}')
            WHERE {" AND ".join(where)}
        ) WHERE rn = 1
        ORDER BY security_id, session_date
        """
        return self.con.execute(sql, args).fetchall()

    def _fetch_actions(self, security_ids, as_of):
        """PIT-filtered corporate actions for `security_ids` (ex_date <= as_of
        AND knowledge_time <= as_of), read through the Postgres attach. Returns
        a list of adjust.Action. Requires connect(attach_postgres=True)."""
        from .adjust import Action
        if not security_ids:
            return []
        as_of_d = as_of.date() if isinstance(as_of, datetime) else as_of
        ph = ",".join("?" for _ in security_ids)
        rows = self.con.execute(
            f"""
            SELECT ca.security_id, ca.action_type, ca.ex_date,
                   ca.split_from, ca.split_to, ca.cash_amount, ds.name
            FROM pg.corp.corporate_action ca
            JOIN pg.ref.data_source ds ON ds.source_id = ca.source_id
            WHERE ca.security_id IN ({ph})
              AND ca.ex_date <= ?
              AND ca.knowledge_time <= ?
            ORDER BY ca.ex_date
            """,
            [*security_ids, as_of_d, self._as_of_ts(as_of)],
        ).fetchall()
        pairs = [
            (
                r[6],  # source name
                Action(
                    security_id=r[0], action_type=r[1], ex_date=r[2],
                    split_from=(float(r[3]) if r[3] is not None else None),
                    split_to=(float(r[4]) if r[4] is not None else None),
                    cash_amount=(float(r[5]) if r[5] is not None else None),
                ),
            )
            for r in rows
        ]
        return self._resolve_source_precedence(pairs)
    
    # Source precedence (DEC-019). Sources coexist (append-only); resolution is
    # read-time, per action. When multiple sources carry an action for the same
    # (security_id, action_type, ex_date), keep only the highest-precedence
    # source's copy — this deduplicates identical-payload multi-source actions
    # (e.g. BOOTSTRAP + TIINGO on the same dividend) so a factor is never applied
    # twice. When payloads DIFFER across sources, precedence still selects one
    # (highest-precedence wins); the disagreement itself is surfaced by the
    # corporate-actions DQ check, not silently reconciled here.
    _SOURCE_PRECEDENCE = {"MANUAL": 0, "SEC": 1, "TIINGO": 2, "BOOTSTRAP": 3}

    def _resolve_source_precedence(self, pairs):
        """pairs: list of (source_name, Action). Returns a list of Action with
        one action per (security_id, action_type, ex_date), chosen from the
        highest-precedence source present."""
        best: dict[tuple, tuple[int, "object"]] = {}
        for source_name, action in pairs:
            key = (action.security_id, action.action_type, action.ex_date)
            rank = self._SOURCE_PRECEDENCE.get(source_name, 99)
            current = best.get(key)
            if current is None or rank < current[0]:
                best[key] = (rank, action)
        # preserve ex_date ordering (the query already ORDER BY ex_date)
        return [a for _, a in sorted(
            best.values(), key=lambda ra: ra[1].ex_date)]

    @staticmethod
    def _as_of_ts(as_of):
        """Normalize as_of to a tz-aware UTC timestamp for comparison against
        knowledge_time. A plain date means end-of-day UTC."""
        if isinstance(as_of, datetime):
            return as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        return datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=timezone.utc)

    # ---- helpers ----
    def _macro_glob(self) -> str:
        return (self.config.macro_silver_dir / "*.parquet").as_posix()
        
    def _bars_glob(self) -> str:
        return (self.config.bars_eod_dir / "*.parquet").as_posix()