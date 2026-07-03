"""
Bootstrap loader: Tiingo corporate actions -> corp.corporate_action.

This is the CANONICAL write path for seeding corporate actions until the full
Phase-3 Tiingo connector exists. It lives with the connector code (client.py,
actions.py) so that connector will grow around it rather than move it.

Responsibilities (one, clearly): fetch via the Tiingo client, normalize via
derive_actions, and INSERT into corp.corporate_action. It computes nothing and
validates nothing.

Conflict policy (BOOTSTRAP):
  Three outcomes per incoming action, keyed on the full payload:
    1. exact match (same security, type, ex_date, AND payload)   -> skip quietly
    2. same (security, type, ex_date) but DIFFERENT payload       -> warn + skip
    3. no row for that (security, type, ex_date)                  -> insert
  Outcome 2 warns and skips because we never want an automated bootstrap write
  to create a SECOND, conflicting action on the same ex-date (that would
  double-count in adjustment). This is a BOOTSTRAP POLICY, not a permanent
  architectural rule: when the production Tiingo connector is built, a legitimate
  vendor revision may instead be layered as a NEW bitemporal row with a later
  knowledge_time, preserving the append-only model. corp.corporate_action is
  append-only (V0 mutation-deny trigger); the loader never updates or deletes.

Idempotency key (payload-inclusive, so a vendor revision is NOT mistaken for an
already-loaded action):
    dividend: (security_id, action_type, ex_date, cash_amount, source_id)
    split:    (security_id, action_type, ex_date, split_from, split_to, source_id)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import psycopg

from trading_os.config import settings
from trading_os.engine.adjust import Action

from .actions import derive_actions
from .client import TiingoClient
from .config import TiingoConfig
from trading_os.corp.action_write import write_actions

BOOTSTRAP_SOURCE = "BOOTSTRAP"
# Prices are the ex-date's raw close (Tiingo divCash is on the ex-date); we
# stamp a knowledge_time so PIT queries as-of "now" see these bootstrap actions.
# Bootstrap actions are known "as of the load", not the ex-date — but we set
# knowledge_time to the ex_date so historical as-of queries can also use them.
# (Bootstrap simplification; the production connector will use the true
# announcement/known time.)


@dataclass
class LoadStats:
    symbol: str
    fetched: int          # actions derived from Tiingo
    inserted: int
    skipped_exact: int
    conflicts: int        # same date, different payload (warned + skipped)
    warnings: list[str]


def _ensure_bootstrap_source(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "select source_id from ref.data_source where name = %s", (BOOTSTRAP_SOURCE,)
    ).fetchone()
    if row:
        return row[0]
    return conn.execute(
        """
        insert into ref.data_source
            (name, kind, is_redistributable, base_url, license_notes)
        values (%s, 'corporate_actions', false, '',
                'Hand/bootstrap-loaded corporate actions from Tiingo; supersede with the production connector.')
        returning source_id
        """,
        (BOOTSTRAP_SOURCE,),
    ).fetchone()[0]


def _resolve_security_id(conn: psycopg.Connection, ticker: str) -> int | None:
    row = conn.execute(
        "select sec.resolve_ticker(%s, current_date)", (ticker,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _load_symbol(conn: psycopg.Connection, source_id: int, ticker: str,
                 actions: list[Action]) -> LoadStats:
    # Bootstrap knowledge_time = ex_date (bootstrap simplification; the canonical
    # Tiingo connector uses ingest time per DEC-018). Since actions can span many
    # ex-dates, we write each with its own ex-date-based knowledge_time.
    st = LoadStats(ticker, len(actions), 0, 0, 0, [])
    for a in actions:
        kt = datetime(a.ex_date.year, a.ex_date.month, a.ex_date.day,
                      tzinfo=timezone.utc)
        res = write_actions(conn, [a], source_id, kt, label=ticker)
        st.inserted += res.inserted
        st.skipped_exact += res.skipped_exact
        st.conflicts += res.conflicts
        st.warnings.extend(res.warnings)
    return st

def bootstrap(symbols: list[str], start: date, end: date | None = None,
              config: TiingoConfig | None = None) -> list[LoadStats]:
    """
    Fetch Tiingo actions for `symbols` and load them into corp.corporate_action
    (bootstrap policy). Idempotent: re-running skips exact matches and warns on
    conflicts. Returns per-symbol LoadStats.
    """
    client = TiingoClient(config)
    results: list[LoadStats] = []
    with psycopg.connect(settings.pg_conninfo()) as conn:
        source_id = _ensure_bootstrap_source(conn)
        for sym in symbols:
            sid = _resolve_security_id(conn, sym)
            if sid is None:
                results.append(LoadStats(sym, 0, 0, 0, 0,
                                         [f"{sym}: not in security master; skipped"]))
                continue
            rows = client.fetch_daily(sym, start, end)
            actions, derive_warns = derive_actions(sid, rows)
            st = _load_symbol(conn, source_id, sym, actions)
            st.warnings = derive_warns + st.warnings
            results.append(st)
        conn.commit()
    return results