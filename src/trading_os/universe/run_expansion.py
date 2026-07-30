"""
Migration A runner: create the survivorship-expansion identities (DEC-017).

Repo path: src/trading_os/universe/run_expansion.py
Run:
  dry-run (default, ZERO writes):  python -m trading_os.universe.run_expansion
  commit:                          python -m trading_os.universe.run_expansion --commit

Reads the APPROVED manifest (manifests/002_survivorship_expansion.csv) through the
universe layer — the sole identity creator (DEC-017) — and creates each security
with its HONEST valid_from/valid_to via UniverseWriter.create_dated. CIK is
resolved from SEC where the issuer is a current filer (most delisted names won't
resolve; that's expected). Additive INSERTs only (Migration A); the existing 527
sentinels are untouched (that's the separate Migration B).

Safety:
  - dry-run is the default: every proposed identity + its collision-check result
    is printed, NOTHING is written.
  - --commit wraps all creates in ONE transaction that ROLLS BACK ENTIRELY on any
    COLLISION (interval-overlap with a different security). Identity corruption is
    not recoverable, so the whole run aborts rather than writing a partial set.
  - idempotent: re-running after a successful commit creates nothing new.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone

import psycopg

from trading_os.config import settings
from . import config as ucfg
from .coverage import load_manifest
from .writer import UniverseWriter

MANIFEST_FILE = "002_survivorship_expansion.csv"


def _resolve_ciks(config: ucfg.UniverseConfig) -> dict[str, str]:
    """ticker(upper) -> zero-padded CIK from SEC company_tickers.json (current
    filers only; defunct issuers legitimately absent). Same source/convention as
    the classifier. A network failure here is non-fatal: we proceed with no CIKs
    rather than block identity creation on optional issuer metadata."""
    try:
        req = urllib.request.Request(
            config.company_tickers_url, headers={"User-Agent": config.sec_user_agent}
        )
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return {row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in data.values()}
    except Exception as e:  # noqa: BLE001
        print(f"[expansion] WARNING: CIK fetch failed ({e}); proceeding with no CIKs.")
        return {}


def run(commit: bool) -> int:
    config = ucfg.UniverseConfig()
    manifest_path = config.manifest_dir / MANIFEST_FILE
    entries = load_manifest(manifest_path, default_type="EQUITY")
    if not entries:
        print(f"[expansion] no entries in {manifest_path}; nothing to do.")
        return 1
    ciks = _resolve_ciks(config)

    # Sanity: honest-window invariant must hold for every row (the emitter already
    # guarantees this; we re-check because this is the identity write).
    bad = [e.ticker for e in entries if e.valid_from is None]
    if bad:
        print(f"[expansion] ABORT: {len(bad)} entries lack valid_from: {bad[:10]}")
        return 1

    print(f"[expansion] manifest: {manifest_path.name}  entries: {len(entries)}  "
          f"CIKs available: {sum(1 for e in entries if e.ticker.upper() in ciks)}")
    print(f"[expansion] mode: {'COMMIT' if commit else 'DRY-RUN (no writes)'}\n")

    tallies = {"created": 0, "skipped_exists": 0, "COLLISION": 0}
    collisions: list[str] = []
    proposed: list[tuple] = []

    with psycopg.connect(settings.pg_conninfo()) as conn:
        writer = UniverseWriter(conn, config)
        source_id = writer.ensure_source()
        batch_id = writer.open_batch(
            datetime.now(timezone.utc), source_id,
            {"migration": "A", "manifest": manifest_path.name, "count": len(entries),
             "commit": commit},
        )

        for e in entries:
            cik = ciks.get(e.ticker.upper())
            if commit:
                status = writer.create_dated(e, cik, source_id, batch_id)
            else:
                # dry-run: run ONLY the read-side collision check, never insert.
                col = writer._ticker_collision(e.ticker, e.valid_from, e.valid_to)
                status = "COLLISION" if col is not None else "created"
            tallies[status] = tallies.get(status, 0) + 1
            if status == "COLLISION":
                collisions.append(e.ticker)
            proposed.append((e.ticker, e.valid_from, e.valid_to, cik or "-", status))

            if commit and status == "COLLISION":
                # Abort immediately: roll back the whole transaction.
                writer.close_batch(batch_id, "failed", rows_in=len(entries), rows_out=0,
                                   error=f"collision on {e.ticker}")
                conn.rollback()
                print(f"[expansion] COLLISION on {e.ticker!r} — ROLLED BACK. No identities "
                      f"written. Resolve the collision and re-run.")
                _report(proposed, tallies, collisions)
                return 2

        if commit:
            if collisions:
                conn.rollback()
                print("[expansion] collisions present — rolled back.")
                _report(proposed, tallies, collisions)
                return 2
            writer.close_batch(batch_id, "succeeded",
                               rows_in=len(entries), rows_out=tallies["created"])
            conn.commit()
            print(f"[expansion] COMMITTED. created={tallies['created']} "
                  f"skipped_exists={tallies['skipped_exists']}")
        else:
            conn.rollback()  # dry-run: discard the batch row too

    _report(proposed, tallies, collisions)
    return 0 if not collisions else 2


def _report(proposed, tallies, collisions) -> None:
    print("\n=== migration A report ===")
    print(f"  created:         {tallies.get('created', 0)}")
    print(f"  skipped_exists:  {tallies.get('skipped_exists', 0)}")
    print(f"  COLLISIONS:      {tallies.get('COLLISION', 0)}  {collisions[:15]}")
    print(f"  total proposed:  {len(proposed)}")
    # show a sample of proposed identities (dry-run inspection)
    print("\n  sample proposed identities:")
    for t, vf, vt, cik, st in proposed[:12]:
        print(f"    {t:7} {str(vf)}..{str(vt) if vt else 'open':10} cik={cik:11} {st}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trading_os.universe.run_expansion")
    ap.add_argument("--commit", action="store_true",
                    help="write identities (default: dry-run, no writes)")
    args = ap.parse_args(argv)
    return run(commit=args.commit)


if __name__ == "__main__":
    raise SystemExit(main())