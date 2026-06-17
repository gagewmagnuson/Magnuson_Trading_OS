"""
Typed data shapes shared across the connector. Keeping these in one place is
what prevents interface drift between client -> parser -> mapper -> writer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class BronzeRef:
    """Pointer to an immutable bronze file on disk (DEC-012)."""
    ticker: str
    cik: str
    path: str               # absolute or repo-relative path to the JSON
    downloaded_at: datetime  # wall clock of download (NOT knowledge_time)


@dataclass(frozen=True)
class RawFact:
    """
    One observation as it appears in Company Facts, before mapping.

    Company Facts groups values under facts[taxonomy][tag]['units'][unit] = [ ...
    entries ]. Each entry has 'end' (and 'start' for durations), 'val', 'accn'
    (accession), 'form', 'fy', 'fp', 'filed', and a 'frame' sometimes.

    period_start is None for instants (balance-sheet); set for durations (flows).
    knowledge_time is derived from the SEC acceptance timestamp at parse time;
    see parser for how 'filed' + accession resolve to it.
    """
    taxonomy: str
    tag: str
    unit: str
    value: float
    period_start: date | None
    period_end: date
    form: str
    fiscal_year: int | None
    fiscal_period: str | None     # 'Q1'..'Q4','FY'
    accession: str
    filed_date: date              # 'filed' field (date only)


@dataclass(frozen=True)
class MappedFact:
    """A RawFact resolved to a canonical concept, ready for the writer."""
    concept_id: int
    canonical_name: str
    source_tag: str
    mapping_confidence: str       # HIGH | MEDIUM | LOW
    prefer_higher_confidence: bool  # DEC-013 per-concept conflict policy
    expected_unit: str | None
    value: float
    unit: str
    period_start: date | None
    period_end: date
    form: str
    fiscal_period: str | None
    accession: str
    filed_date: date


@dataclass(frozen=True)
class ConceptConflict:
    """Two valid mapped tags for one concept/period that materially disagree."""
    concept_id: int
    canonical_name: str
    period_start: date | None
    period_end: date
    tag_a: str
    value_a: float
    tag_b: str
    value_b: float
    unit: str


@dataclass(frozen=True)
class UnmappedTag:
    """A us-gaap tag with no alias mapping. Logged, never silently dropped."""
    tag: str
    value: float | None
    unit: str | None
    context_ref: str | None