"""
Point-in-time knowledge_time derivation for EOD bars (DEC-024, Rule 1).

Repo path: src/trading_os/bars/knowledge_time.py

knowledge_time for an EOD bar is the earliest moment the bar was OBJECTIVELY
KNOWABLE in the market — the exchange session close on session_date, in UTC — NOT
when this system fetched it. This is DATASET-scoped and SOURCE-independent: every
bars connector (Alpaca, Tiingo, ...) derives knowledge_time through this one
function, so Alpaca and Tiingo bars carry identical semantics and remain
interchangeable under DEC-019 precedence.

Calendar: XNYS (NYSE). Per DEC-008 the universe is US equities/ETFs, and XNYS and
XNAS share identical session hours (9:30-16:00 ET, same holidays, same ~13:00 ET
half-day early closes), so the XNYS session schedule is THE US-equity trading day.
This is a documented simplification: when non-US or derivative instruments arrive
(V2+), a per-security calendar lookup replaces this single-calendar assumption.

Half-days and DST are handled by `exchange_calendars`, not by us: the library
returns each session's true close as a tz-aware UTC timestamp, so a 13:00 ET early
close yields 18:00 UTC and a normal 16:00 ET close yields 20:00/21:00 UTC across
the DST boundary — automatically. Hardcoding 16:00 ET would silently overclaim
knowability on the ~8 half-days per year.

STRICT BY DESIGN: a session_date that is not a valid XNYS trading session raises
NonSessionDateError. A bar for a closed-market date is a vendor-data defect, and
fabricating a close time for it would be a lie. The INGESTION layer is responsible
for catching this, emitting a DQ event, skipping the one bar, and continuing (so a
single bad vendor bar never fails a multi-million-row backfill) — but this
primitive never guesses. Strict core, graceful boundary.
"""
from __future__ import annotations

from datetime import date, datetime

import exchange_calendars as xcals

_CALENDAR_CODE = "XNYS"

# Module-level calendar instance: construction is relatively expensive, and the
# schedule is deterministic for a pinned library version, so build once and reuse
# across the millions of calls a backfill makes.
_cal = xcals.get_calendar(_CALENDAR_CODE)


class NonSessionDateError(ValueError):
    """Raised when a bar's session_date is not a valid XNYS trading session."""


def market_close_knowledge_time(session_date: date) -> datetime:
    """Return the XNYS session close for session_date as a tz-aware UTC datetime.

    This is the bar's knowledge_time under DEC-024 Rule 1: the earliest moment the
    bar was objectively knowable (exchange close), not the ingest time.

    Raises NonSessionDateError if session_date is not a trading session (holiday,
    weekend, or a date outside the calendar's range).
    """
    ts = _iso(session_date)
    if not _cal.is_session(ts):
        raise NonSessionDateError(
            f"{session_date.isoformat()} is not an XNYS trading session"
        )
    # exchange_calendars returns the session close as a tz-aware UTC Timestamp;
    # honors half-day early closes automatically.
    close = _cal.session_close(ts)
    return close.to_pydatetime()


def _iso(session_date: date) -> str:
    """exchange_calendars accepts ISO date strings for session lookups."""
    return session_date.isoformat()