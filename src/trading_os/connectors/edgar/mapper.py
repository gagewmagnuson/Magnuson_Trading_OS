"""
Resolve raw us-gaap tags to canonical concepts using fund.concept_alias.

Loads the alias table once (priority + confidence + expected_unit) and applies
the DEC-009 rules:
  * A tag maps to a concept only if present in fund.concept_alias.
  * Unknown tags are returned as UnmappedTag (logged, never dropped).
  * When MULTIPLE mapped tags resolve to the SAME concept for the SAME
    (period_start, period_end), the connector must not silently pick one.
    The mapper surfaces every mapped fact; the WRITER detects same-concept/
    same-period disagreement and logs a ConceptConflict, skipping the value.
  * Unit validation against expected_unit happens here: a value whose unit
    does not match the concept's expected_unit is rejected (returned as
    unmapped with a marker) rather than stored wrong.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import MappedFact, RawFact, UnmappedTag


# Company Facts unit strings vs our canonical expected_unit vocabulary.
# expected_unit uses 'USD', 'USD/shares', 'shares'. Company Facts uses
# 'USD', 'USD/shares', 'shares' as well, but per-share can appear as
# 'USD/shares'. We normalize defensively.
def _unit_matches(reported_unit: str, expected_unit: str | None) -> bool:
    if expected_unit is None:
        return True
    r = reported_unit.strip()
    if r == expected_unit:
        return True
    # tolerate case/format variants
    norm = {"usd": "USD", "usd/shares": "USD/shares", "shares": "shares"}
    return norm.get(r.lower(), r) == expected_unit


@dataclass(frozen=True)
class _Alias:
    concept_id: int
    canonical_name: str
    priority: int
    confidence: str
    expected_unit: str | None
    prefer_higher_confidence: bool


class ConceptMapper:
    def __init__(self, alias_index: dict[str, _Alias]):
        # alias_index: us-gaap tag -> _Alias (already best-priority per tag)
        self._by_tag = alias_index

    @classmethod
    def from_rows(cls, rows: list[dict]) -> "ConceptMapper":
        """
        Build from fund.concept_alias joined to fund.concept rows.
        Each row: source_tag, concept_id, canonical_name, priority,
                  mapping_confidence, expected_unit.
        If the same tag appears twice (shouldn't, given unique constraint),
        keep the lower-priority number (higher precedence).
        """
        idx: dict[str, _Alias] = {}
        for r in rows:
            tag = r["source_tag"]
            cand = _Alias(
                concept_id=r["concept_id"],
                canonical_name=r["canonical_name"],
                priority=r["priority"],
                confidence=r["mapping_confidence"],
                expected_unit=r["expected_unit"],
                prefer_higher_confidence=r["prefer_higher_confidence"],
            )
            existing = idx.get(tag)
            if existing is None or cand.priority < existing.priority:
                idx[tag] = cand
        return cls(idx)

    def map_fact(self, raw: RawFact) -> MappedFact | UnmappedTag:
        alias = self._by_tag.get(raw.tag)
        if alias is None:
            return UnmappedTag(
                tag=raw.tag, value=raw.value, unit=raw.unit, context_ref=None
            )
        if not _unit_matches(raw.unit, alias.expected_unit):
            # Unit mismatch: do not store wrong. Log as unmapped with the unit
            # preserved so review can see what happened.
            return UnmappedTag(
                tag=f"{raw.tag}#UNIT_MISMATCH(expected={alias.expected_unit},got={raw.unit})",
                value=raw.value, unit=raw.unit, context_ref=None
            )
        return MappedFact(
            concept_id=alias.concept_id,
            canonical_name=alias.canonical_name,
            source_tag=raw.tag,
            mapping_confidence=alias.confidence,
            prefer_higher_confidence=alias.prefer_higher_confidence,
            expected_unit=alias.expected_unit,
            value=raw.value,
            unit=raw.unit,
            period_start=raw.period_start,
            period_end=raw.period_end,
            form=raw.form,
            fiscal_period=raw.fiscal_period,
            accession=raw.accession,
            filed_date=raw.filed_date,
        )