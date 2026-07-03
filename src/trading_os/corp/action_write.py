"""
The single canonical write path for corp.corporate_action.

Every corporate-action source (the bootstrap loader, the Tiingo connector, and
any future Polygon/SEC/manual source) writes through write_action() so the
append-only semantics, the payload-inclusive idempotency key, and the
conflict-detection policy have exactly ONE implementation.

Three outcomes per incoming action, keyed on the full payload within a source:
  1. exact match   (same security, type, ex_date, source AND payload) -> SKIP
  2. conflict      (same security, type, ex_date, source; DIFFERENT payload)
                   -> WARN + SKIP  (bootstrap/default policy)
  3. new           (no row for that security/type/ex_date/source)      -> INSERT

corp.corporate_action is append-only (V0 mutation-deny trigger); this primitive
never updates or deletes. Callers pass their own source_id, so ownership is
explicit and one source never touches another source's rows (DEC-019).

knowledge_time: callers pass it. Canonical connectors use INGEST time (DEC-018);
the ex_date is the price-effect anchor, not the knowledge anchor.

Conflict policy note (bootstrap vs production): warn-and-skip is the safe
default so an automated run never creates a second, conflicting action on the
same ex-date (which would double-count in adjustment). A production source MAY
instead choose to layer a genuine vendor revision as a NEW row with a later
knowledge_time — that is a deliberate caller decision, not this primitive's
default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import psycopg

from trading_os.engine.adjust import Action

# Outcome tags returned per action.
INSERTED = "inserted"
SKIPPED_EXACT = "skipped_exact"
CONFLICT = "conflict"


@dataclass
class WriteResult:
    inserted: int = 0
    skipped_exact: int = 0
    conflicts: int = 0
    warnings: list[str] = field(default_factory=list)

    def add(self, other: "WriteResult") -> None:
        self.inserted += other.inserted
        self.skipped_exact += other.skipped_exact
        self.conflicts += other.conflicts
        self.warnings.extend(other.warnings)


def _num_eq(a, b, tol: float = 1e-9) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def _payload_matches(existing: tuple, a: Action) -> bool:
    """existing = (split_from, split_to, cash_amount) from an existing DB row."""
    ef, et, ec = existing
    if a.action_type == "SPLIT":
        return _num_eq(ef, a.split_from) and _num_eq(et, a.split_to)
    return _num_eq(ec, a.cash_amount)


def _describe(existing: tuple, action_type: str) -> str:
    sf, st, cc = existing
    return f"{sf}->{st}" if action_type == "SPLIT" else f"${cc}"


def _describe_action(a: Action) -> str:
    return (f"{a.split_from}->{a.split_to}" if a.action_type == "SPLIT"
            else f"${a.cash_amount}")


def write_action(
    conn: psycopg.Connection,
    action: Action,
    source_id: int,
    knowledge_time: datetime,
    label: str = "",
) -> str:
    """
    Write ONE action under `source_id` with the append-only three-outcome policy.
    Returns one of INSERTED / SKIPPED_EXACT / CONFLICT. On CONFLICT the caller
    should surface the warning (see write_actions, which collects them).
    `label` (e.g. a ticker) is only used to make warnings readable.
    """
    existing = conn.execute(
        """
        select split_from, split_to, cash_amount
        from corp.corporate_action
        where security_id = %s and action_type = %s and ex_date = %s
          and source_id = %s
        """,
        (action.security_id, action.action_type, action.ex_date, source_id),
    ).fetchall()

    if existing:
        if any(_payload_matches(r, action) for r in existing):
            return SKIPPED_EXACT                                  # outcome 1
        return CONFLICT                                           # outcome 2 (caller warns)

    conn.execute(                                                 # outcome 3
        """
        insert into corp.corporate_action
            (security_id, action_type, ex_date, split_from, split_to,
             cash_amount, knowledge_time, source_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (action.security_id, action.action_type, action.ex_date,
         action.split_from, action.split_to, action.cash_amount,
         knowledge_time, source_id),
    )
    return INSERTED


def write_actions(
    conn: psycopg.Connection,
    actions: list[Action],
    source_id: int,
    knowledge_time: datetime,
    label: str = "",
) -> WriteResult:
    """
    Write a batch of actions under one source_id/knowledge_time. Collects the
    three-outcome tallies and conflict warnings. Does NOT commit — the caller
    controls the transaction boundary.
    """
    res = WriteResult()
    for a in actions:
        outcome = write_action(conn, a, source_id, knowledge_time, label)
        if outcome == INSERTED:
            res.inserted += 1
        elif outcome == SKIPPED_EXACT:
            res.skipped_exact += 1
        else:  # CONFLICT
            res.conflicts += 1
            existing = conn.execute(
                """
                select split_from, split_to, cash_amount
                from corp.corporate_action
                where security_id = %s and action_type = %s and ex_date = %s
                  and source_id = %s
                """,
                (a.security_id, a.action_type, a.ex_date, source_id),
            ).fetchall()
            desc = "; ".join(_describe(r, a.action_type) for r in existing)
            res.warnings.append(
                f"CONFLICT {label} {a.action_type} {a.ex_date}: "
                f"existing [{desc}] != incoming [{_describe_action(a)}] "
                f"-> skipped (append-only; no mutation)"
            )
    return res