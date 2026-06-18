"""Typed shapes shared across the FRED connector (prevents interface drift)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class BronzeRef:
    """Pointer to an immutable bronze ALFRED file on disk (DEC-012)."""
    series_id: str
    path: str
    downloaded_at: datetime


@dataclass(frozen=True)
class SeriesMeta:
    """Series-level metadata from the FRED series endpoint."""
    series_id: str
    title: str
    units: str | None
    frequency: str | None        # 'Daily','Monthly','Quarterly',...
    seasonal_adjustment: str | None


@dataclass(frozen=True)
class VintageObs:
    """
    One observation at one vintage, before writing.

    obs_date     = the period the value refers to (event_time).
    vintage_date = realtime_start = when this value became the published figure
                   (knowledge_time). Multiple vintages per obs_date = revisions.
    value        = the figure (None for ALFRED missing-value '.').
    """
    series_id: str
    obs_date: date
    vintage_date: date
    realtime_end: date | None
    value: float | None