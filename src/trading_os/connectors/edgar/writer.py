"""
Bitemporal writer. Owns all Postgres interaction for the connector.

Responsibilities:
  * Open an ingest_batch row, stamp it on every fact written, close it.
  * Resolve security_id from ticker (creating a minimal sec.security +
    TICKER identifier on first sight, since V0 has no OpenFIGI seed yet).
  * Insert fund.filing rows (one per accession) and fund.fundamental_fact
    rows (append-only; the schema trigger blocks UPDATE/DELETE).
  * Detect same-concept/same-period disagreement among mapped facts and log
    fund.concept_conflict instead of guessing (DEC-009 Rule #4).
  * Log fund.unmapped_tag for every unmapped/unit-mismatched tag.

knowledge_time = SEC acceptance timestamp. Company Facts exposes 'filed' as a
DATE only (no time). V0 uses filed_date at 00:00 UTC as knowledge_time; this is
a deliberate, documented approximation (see DEC-013 candidate in the CLI notes).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import psycopg

from .config import CONFLICT_REL_TOLERANCE, EdgarConfig
from .models import ConceptConflict, MappedFact, UnmappedTag


class FactWriter:
    def __init__(self, conn: psycopg.Connection, config: EdgarConfig):
        self.conn = conn
        self.config = config

    # ---- batch lifecycle -------------------------------------------------
    def open_batch(self, dataset: str, knowledge_time: datetime, params: dict) -> int:
        row = self.conn.execute(
            """
            insert into meta.ingest_batch
                (source_id, dataset, knowledge_time, params, code_version, status)
            values (
                (select source_id from ref.data_source where name = 'SEC_EDGAR'),
                %s, %s, %s, %s, 'running')
            returning batch_id
            """,
            (dataset, knowledge_time, psycopg.types.json.Json(params), "edgar-v0"),
        ).fetchone()
        return row[0]

    def close_batch(self, batch_id: int, status: str, rows_in: int, rows_out: int,
                    error: str | None = None) -> None:
        self.conn.execute(
            """
            update meta.ingest_batch
               set status = %s, finished_at = now(),
                   rows_in = %s, rows_out = %s, error = %s
             where batch_id = %s
            """,
            (status, rows_in, rows_out, error, batch_id),
        )

    # ---- security resolution (minimal, V0) -------------------------------
    def resolve_or_create_security(self, ticker: str, batch_id: int) -> int:
        """
        V0 has no OpenFIGI seed yet, so we lazily create a minimal security
        and a TICKER identifier the first time we see a ticker. figi is left
        NULL; the unique(figi) constraint permits multiple NULLs in Postgres.
        """
        found = self.conn.execute(
            "select sec.resolve_ticker(%s, current_date)",
            (ticker,),
        ).fetchone()
        if found and found[0] is not None:
            return found[0]

        sec_id = self.conn.execute(
            """
            insert into sec.security (security_type, description, source_id)
            values ('EQUITY', %s,
                    (select source_id from ref.data_source where name='SEC_EDGAR'))
            returning security_id
            """,
            (f"{ticker} (created by EDGAR connector)",),
        ).fetchone()[0]

        self.conn.execute(
            """
            insert into sec.security_identifier
                (security_id, id_type, id_value, valid_from, knowledge_time, batch_id)
            values (%s, 'TICKER', %s, date '1900-01-01', now(), %s)
            """,
            (sec_id, ticker, batch_id),
        )
        return sec_id

    # ---- filings ---------------------------------------------------------
    def upsert_filing(self, security_id: int, accession: str, form: str,
                      period_end, fiscal_period, filed_at: datetime,
                      batch_id: int) -> int:
        """
        One filing row per accession. unique(accession_number) makes this
        idempotent: re-running returns the existing filing_id.
        """
        existing = self.conn.execute(
            "select filing_id from fund.filing where accession_number = %s",
            (accession,),
        ).fetchone()
        if existing:
            return existing[0]
        return self.conn.execute(
            """
            insert into fund.filing
                (security_id, form_type, period_end_date, fiscal_period,
                 filed_at, accession_number, source_id, batch_id)
            values (%s, %s, %s, %s, %s, %s,
                    (select source_id from ref.data_source where name='SEC_EDGAR'),
                    %s)
            returning filing_id
            """,
            (security_id, form, period_end, fiscal_period, filed_at, accession, batch_id),
        ).fetchone()[0]

    # ---- conflict detection + fact write ---------------------------------
    def write_facts(self, security_id: int, mapped: list[MappedFact],
                    batch_id: int) -> tuple[int, list[ConceptConflict]]:
        """
        Write facts bitemporally.

        Conflict detection (DEC-009 Rule #4) applies ONLY WITHIN a single
        filing (accession): if two DIFFERENT mapped tags in the SAME filing
        resolve to the same concept/period and materially disagree, we cannot
        tell which the filer meant, so we log a conflict and skip that group.

        ACROSS filings, the same concept/period legitimately carries different
        values over time (restatements, reclassifications). Those are NOT
        conflicts — they are exactly what the bitemporal model stores: multiple
        rows distinguished by filed_at (knowledge_time). fundamentals_asof()
        later selects the value known as of a given date.

        Grouping key therefore includes accession. Within a group, a conflict
        requires TWO DISTINCT source_tags that disagree; the same tag appearing
        once per filing is the normal case and is written.
        Returns (facts_written, conflicts).
        """
        # Group by the full identity of an observation within one filing.
        groups: dict[tuple, list[MappedFact]] = defaultdict(list)
        for m in mapped:
            groups[(m.accession, m.concept_id, m.period_start, m.period_end)].append(m)

        written = 0
        conflicts: list[ConceptConflict] = []

        for (accession, concept_id, p_start, p_end), items in groups.items():
            # A genuine in-filing conflict needs two DIFFERENT tags that
            # materially disagree. Same tag (or agreeing tags) => no conflict.
            distinct_tags = {i.source_tag for i in items}
            disagree = len(distinct_tags) > 1 and _materially_differ(items)

            if disagree:
                resolved = _resolve_by_confidence(items)
                if resolved is not None:
                    # DEC-013: this concept's priority order encodes a proven
                    # scope hierarchy (prefer_higher_confidence=true) AND a
                    # unique highest-confidence tag exists. Take it silently;
                    # do NOT log a conflict.
                    chosen = resolved
                else:
                    # Either the concept is strict (flag false) or the top
                    # confidence is tied. Genuine ambiguity -> log + skip.
                    a, b = _two_disagreeing(items)
                    conflicts.append(ConceptConflict(
                        concept_id=concept_id, canonical_name=a.canonical_name,
                        period_start=p_start, period_end=p_end,
                        tag_a=a.source_tag, value_a=a.value,
                        tag_b=b.source_tag, value_b=b.value, unit=a.unit,
                    ))
                    continue
            else:
                # Agreeing tags, or a single tag. Write one fact for this
                # (filing, concept, period). Different filings -> different
                # rows -> bitemporal history.
                chosen = items[0]
            filing_id = self.upsert_filing(
                security_id, chosen.accession, chosen.form, chosen.period_end,
                chosen.fiscal_period, _knowledge_time(chosen.filed_date), batch_id,
            )
            # Idempotency: skip if this exact (filing, concept, period) fact
            # already exists, so re-running the connector does not duplicate.
            exists = self.conn.execute(
                """
                select 1 from fund.fundamental_fact
                 where filing_id = %s and concept_id = %s
                   and period_end_date = %s
                   and period_start is not distinct from %s
                 limit 1
                """,
                (filing_id, concept_id, p_end, p_start),
            ).fetchone()
            if exists:
                continue
            self.conn.execute(
                """
                insert into fund.fundamental_fact
                    (security_id, filing_id, concept_id, period_start,
                     period_end_date, fiscal_period, value, unit, filed_at, batch_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (security_id, filing_id, concept_id, p_start, p_end,
                 chosen.fiscal_period, chosen.value, chosen.unit,
                 _knowledge_time(chosen.filed_date), batch_id),
            )
            written += 1

        for c in conflicts:
            self._log_conflict(security_id, c, batch_id)
        return written, conflicts

    def _log_conflict(self, security_id: int, c: ConceptConflict, batch_id: int) -> None:
        # filing_id is required by the table; attach the first filing we can
        # find for this conflict's accession context is non-trivial here, so we
        # log against a filing resolved by the conflict's period if present.
        # Simpler + robust: conflicts reference concept/period, not a filing,
        # so we record against any filing for this security in this batch.
        filing = self.conn.execute(
            "select filing_id from fund.filing where security_id=%s order by filed_at desc limit 1",
            (security_id,),
        ).fetchone()
        if not filing:
            return
        self.conn.execute(
            """
            insert into fund.concept_conflict
                (filing_id, concept_id, period_start, period_end_date,
                 tag_a, value_a, tag_b, value_b, unit)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (filing[0], c.concept_id, c.period_start, c.period_end,
             c.tag_a, c.value_a, c.tag_b, c.value_b, c.unit),
        )

    # ---- unmapped --------------------------------------------------------
    def log_unmapped(self, filing_id: int, tags: list[UnmappedTag]) -> None:
        for t in tags:
            self.conn.execute(
                """
                insert into fund.unmapped_tag (filing_id, tag, value, unit, context_ref)
                values (%s, %s, %s, %s, %s)
                """,
                (filing_id, t.tag, t.value, t.unit, t.context_ref),
            )


def _materially_differ(items: list[MappedFact]) -> bool:
    vals = [i.value for i in items]
    lo, hi = min(vals), max(vals)
    base = max(abs(lo), abs(hi), 1.0)
    return (hi - lo) / base > CONFLICT_REL_TOLERANCE


def _knowledge_time(filed_date) -> datetime:
    # filed is a DATE; treat as 00:00 UTC. Documented approximation.
    return datetime(filed_date.year, filed_date.month, filed_date.day, tzinfo=timezone.utc)

def _two_disagreeing(items: list[MappedFact]) -> tuple[MappedFact, MappedFact]:
    """Return two facts with different tags whose values differ the most."""
    lo = min(items, key=lambda i: i.value)
    hi = max(items, key=lambda i: i.value)
    if lo.source_tag == hi.source_tag:
        # Same tag at the extremes; find any differently-tagged pair.
        first = items[0]
        for other in items[1:]:
            if other.source_tag != first.source_tag:
                return first, other
    return lo, hi

# Confidence rank: lower number = stronger. Used by DEC-013 resolution.
_CONFIDENCE_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _resolve_by_confidence(items: list[MappedFact]) -> MappedFact | None:
    """
    DEC-013. If the concept opts into prefer_higher_confidence AND there is a
    UNIQUE strongest-confidence tag among the disagreeing items, return that
    item (it wins silently). Otherwise return None, meaning 'log a conflict'.

    A concept opts in when prefer_higher_confidence is true on ANY of its mapped
    facts (the flag is a concept property, identical across its aliases).
    """
    if not any(i.prefer_higher_confidence for i in items):
        return None
    ranked = sorted(items, key=lambda i: _CONFIDENCE_RANK.get(i.mapping_confidence, 9))
    best_rank = _CONFIDENCE_RANK.get(ranked[0].mapping_confidence, 9)
    top = [i for i in ranked if _CONFIDENCE_RANK.get(i.mapping_confidence, 9) == best_rank]
    # Unique strongest tag (by tag identity) required; ties => ambiguous.
    if len({i.source_tag for i in top}) == 1:
        return top[0]
    return None