"""
Load S&P 500 point-in-time membership (Migration D).

Repo path: src/trading_os/universe/load_membership.py
Run:
  dry-run (default, ZERO writes):  python -m trading_os.universe.load_membership
  commit:                          python -m trading_os.universe.load_membership --commit

Populates univ.universe_membership from the sp500 ticker/start/end file, so
univ.members_asof('SP500', <date>) returns the ACTUAL historical constituents —
including companies that later delisted (the survivorship-free payoff of the
security-master expansion). Each interval's ticker is resolved AS OF its start
date, so a reused ticker resolves to the company that held it THEN (or fails
honestly if that company was never onboarded).

Integrity gates (a membership is written only if BOTH pass):
  1. Resolution: ticker resolves to a security_id as of the interval start
     (sec.resolve_ticker already requires the identity be valid at that date).
  2. Containment: the membership interval lies within the security's identity
     window — in particular the membership must not extend past the security's
     delisting (identity valid_to). A violation means the resolution is suspect;
     skip and report rather than write a contradictory fact.

Append-only + idempotent: univ.universe_membership carries a no-mutation trigger,
so the loader INSERTs only, and only rows that don't already exist
((universe_id, security_id, valid_from) key) — re-running is safe by construction.
One fixed knowledge_time stamps the whole batch (membership is reference data
loaded at one moment, not row-by-row revisions).
"""
from __future__ import annotations

import argparse
import csv
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import psycopg

from trading_os.config import settings
from . import config as ucfg
from .writer import UniverseWriter

SP500_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
)
UNIVERSE_CODE = "SP500"


def _d(s: str | None) -> date | None:
    s = (s or "").strip()
    return date.fromisoformat(s) if s else None


def _load_intervals() -> list[tuple[str, date, date | None]]:
    raw = urllib.request.urlopen(SP500_URL).read().decode()
    out = []
    for r in csv.DictReader(raw.splitlines()):
        tk = (r["ticker"] or "").strip().upper()
        vf = _d(r["start_date"])
        if not tk or vf is None:
            continue
        out.append((tk, vf, _d(r.get("end_date"))))
    return out


def _ensure_universe(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "select universe_id from univ.universe where code = %s", (UNIVERSE_CODE,)
    ).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "insert into univ.universe (code, description) values (%s, %s) returning universe_id",
        (UNIVERSE_CODE, "S&P 500 point-in-time membership (fja05680/sp500)"),
    ).fetchone()[0]


def _resolve_and_check(conn, ticker, vf, vt):
    """Return (security_id, status). status in
    {'ok','no_identity','containment_violation'}."""
    sid = conn.execute(
        "select sec.resolve_ticker(%s, %s::date)", (ticker, vf)
    ).fetchone()[0]
    if sid is None:
        return None, "no_identity"
    # Containment: fetch the resolved identity's window (the one valid at vf).
    ident = conn.execute(
        """
        select valid_from, valid_to
          from sec.security_identifier
         where security_id = %s and id_type = 'TICKER'
           and valid_from <= %s::date
           and (valid_to is null or valid_to > %s::date)
         order by valid_from desc limit 1
        """,
        (sid, vf, vf),
    ).fetchone()
    if ident is None:
        return None, "no_identity"
    id_from, id_to = ident
    # Membership must not extend MATERIALLY past the identity's end. The S&P
    # removal date (sp500 end_date) legitimately lags the last trading day
    # (identity valid_to) by a few days — a company's last trade precedes its
    # index removal when it's acquired/delisted. So allow a grace window that
    # covers normal removal lag; only a gap beyond it signals a real resolution
    # mismatch (wrong security — that error is off by months/years, not days).
    REMOVAL_LAG_GRACE = timedelta(days=15)
    if id_to is not None and vt is not None and vt > id_to + REMOVAL_LAG_GRACE:
        return sid, "containment_violation"
    if id_to is not None and vt is None and id_to < date.today() - REMOVAL_LAG_GRACE:
        # membership open-ended but identity delisted well in the past -> suspect
        return sid, "containment_violation"
    return sid, "ok"


def run(commit: bool) -> int:
    intervals = _load_intervals()
    batch_kt = datetime.now(timezone.utc)   # ONE knowledge_time for the whole batch
    tally = Counter()
    proposed = []
    skips = []

    with psycopg.connect(settings.pg_conninfo()) as conn:
        writer = UniverseWriter(conn, ucfg.UniverseConfig())
        source_id = writer.ensure_source()
        universe_id = _ensure_universe(conn)
        batch_id = conn.execute(
            """
            insert into meta.ingest_batch
                (source_id, dataset, knowledge_time, params, code_version, status)
            values (%s, 'univ.universe_membership', %s, %s, 'membership-v1', 'running')
            returning batch_id
            """,
            (source_id, batch_kt,
             psycopg.types.json.Json({"universe": UNIVERSE_CODE, "intervals": len(intervals),
                                      "commit": commit})),
        ).fetchone()[0]

        for ticker, vf, vt in intervals:
            sid, status = _resolve_and_check(conn, ticker, vf, vt)
            tally[status] += 1
            if status != "ok":
                skips.append((ticker, vf, vt, status))
                continue
            # idempotency: skip if this exact membership already exists
            exists = conn.execute(
                """
                select 1 from univ.universe_membership
                 where universe_id = %s and security_id = %s and valid_from = %s
                """,
                (universe_id, sid, vf),
            ).fetchone()
            if exists:
                tally["already_exists"] += 1
                continue
            proposed.append((ticker, sid, vf, vt))
            if commit:
                conn.execute(
                    """
                    insert into univ.universe_membership
                        (universe_id, security_id, valid_from, valid_to,
                         knowledge_time, batch_id)
                    values (%s, %s, %s, %s, %s, %s)
                    """,
                    (universe_id, sid, vf, vt, batch_kt, batch_id),
                )
                tally["inserted"] += 1

        if commit:
            conn.execute(
                "update meta.ingest_batch set status='succeeded', finished_at=now(), "
                "rows_in=%s, rows_out=%s where batch_id=%s",
                (len(intervals), tally["inserted"], batch_id),
            )
            conn.commit()
        else:
            conn.rollback()

    _report(len(intervals), tally, proposed, skips, commit)
    return 0


def _report(total, tally, proposed, skips, commit):
    print(f"\n=== SP500 membership load ({'COMMIT' if commit else 'DRY-RUN'}) ===")
    print(f"  intervals in file:        {total}")
    print(f"  resolved + contained OK:  {tally['ok']}")
    print(f"    -> would insert:        {len(proposed)}" if not commit else
          f"    -> inserted:            {tally['inserted']}")
    print(f"    -> already existed:     {tally['already_exists']}")
    print(f"  SKIPPED:                  {tally['no_identity'] + tally['containment_violation']}")
    print(f"    no identity as-of start:  {tally['no_identity']}")
    print(f"    containment violation:    {tally['containment_violation']}")
    print("\n  sample proposed memberships:")
    for tk, sid, vf, vt in proposed[:10]:
        print(f"    {tk:7} sid={sid:4} {vf}..{vt or 'current'}")
    if skips:
        print("\n  sample skips:")
        for tk, vf, vt, why in skips[:12]:
            print(f"    {tk:7} {vf}..{vt or 'current':10} {why}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trading_os.universe.load_membership")
    ap.add_argument("--commit", action="store_true", help="write (default: dry-run)")
    return run(commit=ap.parse_args(argv).commit)


if __name__ == "__main__":
    raise SystemExit(main())