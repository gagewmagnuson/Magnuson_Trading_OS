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


def _payload_matches(existing: tuple, a: Action) -> bool:
    """existing = (split_from, split_to, cash_amount) from the DB row."""
    ef, et, ec = existing
    if a.action_type == "SPLIT":
        return (_num_eq(ef, a.split_from) and _num_eq(et, a.split_to))
    return _num_eq(ec, a.cash_amount)


def _num_eq(a, b, tol=1e-9) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def _load_symbol(conn: psycopg.Connection, source_id: int, ticker: str,
                 actions: list[Action]) -> LoadStats:
    st = LoadStats(ticker, len(actions), 0, 0, 0, [])
    for a in actions:
        # Look for any existing action on this (security, type, ex_date, source).
        rows = conn.execute(
            """
            select split_from, split_to, cash_amount
            from corp.corporate_action
            where security_id = %s and action_type = %s and ex_date = %s
              and source_id = %s
            """,
            (a.security_id, a.action_type, a.ex_date, source_id),
        ).fetchall()
        if rows:
            if any(_payload_matches(r, a) for r in rows):
                st.skipped_exact += 1                      # outcome 1
                continue
            # outcome 2: same date, different payload -> warn + skip
            existing_desc = "; ".join(_desc_row(r, a.action_type) for r in rows)
            st.conflicts += 1
            st.warnings.append(
                f"CONFLICT {ticker} {a.action_type} {a.ex_date}: "
                f"existing [{existing_desc}] != incoming [{_desc_action(a)}] "
                f"-> skipping (bootstrap policy)"
            )
            continue
        # outcome 3: insert. knowledge_time = ex_date (bootstrap simplification).
        kt = datetime(a.ex_date.year, a.ex_date.month, a.ex_date.day,
                      tzinfo=timezone.utc)
        conn.execute(
            """
            insert into corp.corporate_action
                (security_id, action_type, ex_date, split_from, split_to,
                 cash_amount, knowledge_time, source_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (a.security_id, a.action_type, a.ex_date,
             a.split_from, a.split_to, a.cash_amount, kt, source_id),
        )
        st.inserted += 1
    return st


def _desc_row(r: tuple, action_type: str) -> str:
    sf, st_, cc = r
    return f"{sf}->{st_}" if action_type == "SPLIT" else f"${cc}"


def _desc_action(a: Action) -> str:
    return f"{a.split_from}->{a.split_to}" if a.action_type == "SPLIT" else f"${a.cash_amount}"


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