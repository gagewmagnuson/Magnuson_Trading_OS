"""
Admin CLI for API-key lifecycle — the sole writer to meta.api_consumer (DEC-021).

Repo path: src/trading_os/api/keys.py
Run as:    python -m trading_os.api.keys <command>

This is administrative tooling, NOT part of the serving API. It is a separate
program run by hand and never exposed over HTTP. It therefore uses its OWN
writable Postgres connection (autocommit) — deliberately NOT deps.get_conn,
which is read-only by design (DEC-022). Two distinct trust boundaries:

    serving API  -> read-only, reachable over HTTP
    this CLI     -> may write metadata, never reachable over HTTP

Hashing is imported from deps.hash_key, so keys are hashed in exactly one place
and the CLI can never disagree with the auth check on the algorithm.

Commands:
    create --label <name>          mint a key; print the raw key ONCE, store only its hash
    list                           show all consumers (never the key — only its hash exists)
    revoke (--id N | --label L)     soft-revoke: is_active=false + revoked_at; row never deleted

Keys are shown exactly once, at creation. Only the SHA-256 hash and a non-secret
prefix are stored; a lost key is re-minted, never recovered.
"""
from __future__ import annotations

import argparse
import secrets
import sys

import psycopg

from trading_os.config import settings
from trading_os.api.deps import hash_key

KEY_PREFIX = "tos_"
PREFIX_STORE_LEN = 12   # non-secret leading chars kept for identification in list/logs


def _admin_conn() -> psycopg.Connection:
    """A WRITABLE admin connection — the only writer to meta.api_consumer.
    Never deps.get_conn (that is read-only, DEC-022); never used over HTTP."""
    conn = psycopg.connect(settings.pg_conninfo())
    conn.autocommit = True
    return conn


def cmd_create(args: argparse.Namespace) -> int:
    raw_key = KEY_PREFIX + secrets.token_urlsafe(32)
    prefix = raw_key[:PREFIX_STORE_LEN]
    with _admin_conn() as conn:
        # Soft nudge: labels are not unique (only key_hash is). Warn on reuse so
        # ambiguity is caught at creation — the friendliest moment — rather than
        # only surfacing later at revoke time.
        dup = conn.execute(
            "SELECT consumer_id FROM meta.api_consumer WHERE label = %s",
            [args.label],
        ).fetchall()
        if dup:
            ids = ", ".join(str(r[0]) for r in dup)
            print(
                f"note: label {args.label!r} is already used by consumer_id {ids}; "
                f"labels need not be unique, but a distinct label avoids ambiguity "
                f"at revoke time.",
                file=sys.stderr,
            )
        cid = conn.execute(
            "INSERT INTO meta.api_consumer (label, key_hash, key_prefix) "
            "VALUES (%s, %s, %s) RETURNING consumer_id",
            [args.label, hash_key(raw_key), prefix],
        ).fetchone()[0]
    print(f"consumer_id: {cid}")
    print(f"label:       {args.label}")
    print(f"API key:     {raw_key}")
    print("This key is shown ONCE and is not stored. Save it now; it cannot be recovered.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with _admin_conn() as conn:
        rows = conn.execute(
            "SELECT consumer_id, label, key_prefix, is_active, created_at, revoked_at "
            "FROM meta.api_consumer ORDER BY consumer_id"
        ).fetchall()
    if not rows:
        print("(no consumers)")
        return 0
    hdr = ("id", "label", "prefix", "active", "created", "revoked")
    print(f"{hdr[0]:>3}  {hdr[1]:<24}  {hdr[2]:<12}  {hdr[3]:<6}  {hdr[4]:<19}  {hdr[5]}")
    for cid, label, prefix, active, created, revoked in rows:
        cstr = created.strftime("%Y-%m-%d %H:%M:%S") if created else ""
        rstr = revoked.strftime("%Y-%m-%d %H:%M:%S") if revoked else ""
        print(f"{cid:>3}  {label:<24}  {prefix:<12}  {str(active):<6}  {cstr:<19}  {rstr}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    with _admin_conn() as conn:
        if args.id is not None:
            target_id = args.id
            exists = conn.execute(
                "SELECT is_active FROM meta.api_consumer WHERE consumer_id = %s",
                [target_id],
            ).fetchone()
            if exists is None:
                print(f"no consumer with id {target_id}", file=sys.stderr)
                return 1
        else:
            matches = conn.execute(
                "SELECT consumer_id, key_prefix, is_active FROM meta.api_consumer "
                "WHERE label = %s ORDER BY consumer_id",
                [args.label],
            ).fetchall()
            if not matches:
                print(f"no consumer with label {args.label!r}", file=sys.stderr)
                return 1
            if len(matches) > 1:
                print(
                    f"label {args.label!r} matches {len(matches)} consumers — "
                    f"disambiguate with --id:",
                    file=sys.stderr,
                )
                for cid, prefix, active in matches:
                    print(f"  id={cid}  prefix={prefix}  active={active}", file=sys.stderr)
                return 2
            target_id = matches[0][0]

        # Soft-revoke only if currently active, so an earlier revoked_at is never
        # overwritten — the original revocation time is preserved (audit trail).
        updated = conn.execute(
            "UPDATE meta.api_consumer SET is_active = false, revoked_at = now() "
            "WHERE consumer_id = %s AND is_active RETURNING consumer_id",
            [target_id],
        ).fetchone()
    if updated is None:
        print(f"consumer_id {target_id} was already revoked; no change.")
    else:
        print(f"revoked consumer_id {target_id}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m trading_os.api.keys",
        description="Admin CLI for serving-API keys (create/list/revoke). "
                    "Sole writer to meta.api_consumer; never exposed over HTTP.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("create", help="mint a key; print it once, store only its hash")
    pc.add_argument("--label", required=True, help="human-readable consumer name")
    pc.set_defaults(func=cmd_create)

    pl = sub.add_parser("list", help="list all consumers (never shows keys)")
    pl.set_defaults(func=cmd_list)

    pr = sub.add_parser("revoke", help="soft-revoke a key (is_active=false)")
    g = pr.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, help="consumer_id (precise; preferred)")
    g.add_argument("--label", help="consumer label (refuses if ambiguous)")
    pr.set_defaults(func=cmd_revoke)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())