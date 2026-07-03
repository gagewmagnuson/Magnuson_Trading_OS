"""
Corporate-actions data-quality check (read-only).

Reports on corp.corporate_action across sources. Like the bars DQ module, it
consumes Postgres directly (psycopg) and never mutates. It answers: how many
securities have actions, what's the split/dividend breakdown per source, are
there cross-source conflicts, and which securities have NO actions (informational
— many legitimately don't).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from trading_os.config import settings


@dataclass
class CorpActionsDQ:
    total_actions: int
    by_source: list[tuple]          # (source, action_type, count)
    secs_with_actions: int
    seeded: int
    secs_without_actions: int
    zero_action_examples: list[str]
    cross_source_conflicts: list[tuple]
    warnings: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        # FAIL only on a true integrity problem: a cross-source conflict where
        # two sources disagree on the SAME action payload (that would make the
        # read-time precedence resolution ambiguous). Coverage gaps are NOT a
        # failure (many securities legitimately have no actions).
        return len(self.cross_source_conflicts) > 0


def run(conn) -> CorpActionsDQ:
    total = conn.execute("select count(*) from corp.corporate_action").fetchone()[0]

    by_source = conn.execute(
        """
        select ds.name, ca.action_type, count(*)
        from corp.corporate_action ca
        join ref.data_source ds on ds.source_id = ca.source_id
        group by ds.name, ca.action_type
        order by ds.name, ca.action_type
        """
    ).fetchall()

    secs_with = conn.execute(
        "select count(distinct security_id) from corp.corporate_action"
    ).fetchone()[0]
    seeded = conn.execute("select count(*) from sec.security").fetchone()[0]

    zero_examples = [r[0] for r in conn.execute(
        """
        select si.id_value
        from sec.security s
        join sec.security_identifier si on si.security_id = s.security_id
           and si.id_type = 'TICKER' and si.valid_from <= current_date
           and (si.valid_to is null or si.valid_to >= current_date)
        where not exists (
            select 1 from corp.corporate_action ca where ca.security_id = s.security_id
        )
        order by si.id_value limit 20
        """
    ).fetchall()]

    # cross-source conflict: same (security, type, ex_date) present in >1 source
    # with a DIFFERENT payload. Same-payload coexistence is fine (expected for
    # BOOTSTRAP vs TIINGO on the cohort); only differing payloads are flagged.
    conflicts = conn.execute(
        """
        select security_id, action_type, ex_date,
               count(distinct coalesce(split_from::text,'') || '|' ||
                     coalesce(split_to::text,'') || '|' ||
                     coalesce(cash_amount::text,'')) as distinct_payloads,
               count(distinct source_id) as sources
        from corp.corporate_action
        group by security_id, action_type, ex_date
        having count(distinct source_id) > 1
           and count(distinct coalesce(split_from::text,'') || '|' ||
                     coalesce(split_to::text,'') || '|' ||
                     coalesce(cash_amount::text,'')) > 1
        limit 50
        """
    ).fetchall()

    return CorpActionsDQ(
        total_actions=total,
        by_source=by_source,
        secs_with_actions=secs_with,
        seeded=seeded,
        secs_without_actions=seeded - secs_with,
        zero_action_examples=zero_examples,
        cross_source_conflicts=conflicts,
    )


def main() -> int:
    with psycopg.connect(settings.pg_conninfo()) as conn:
        r = run(conn)
    print("=" * 60)
    print("CORPORATE ACTIONS DATA QUALITY")
    print("=" * 60)
    print(f"Total actions : {r.total_actions}")
    print(f"Securities    : {r.secs_with_actions} with actions / {r.seeded} seeded "
          f"({r.secs_without_actions} with none)")
    print("By source:")
    for src, atype, cnt in r.by_source:
        print(f"   {src:<10} {atype:<14} {cnt}")
    if r.zero_action_examples:
        print(f"Zero-action securities (informational, first 20): "
              f"{', '.join(r.zero_action_examples)}")
    print("-" * 60)
    if r.cross_source_conflicts:
        print(f"FAIL: {len(r.cross_source_conflicts)} cross-source payload conflicts")
        for sid, atype, ex, npay, nsrc in r.cross_source_conflicts[:10]:
            print(f"   security_id={sid} {atype} {ex}: {npay} payloads across {nsrc} sources")
    else:
        print("Cross-source conflicts: 0")
        print("RESULT: PASS")
    print("=" * 60)
    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())