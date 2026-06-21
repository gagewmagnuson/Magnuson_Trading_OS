"""Typed shapes for the universe layer."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CoverageEntry:
    """One security declared by a coverage manifest."""
    ticker: str
    security_type: str            # 'EQUITY' | 'ETF'
    name: str | None = None


@dataclass(frozen=True)
class CoverageSource:
    """A coverage source declared by one registry row."""
    name: str                     # registry id, e.g. 'sp500'
    path: Path                    # resolved manifest path
    default_type: str             # fallback security_type for rows in this manifest


@dataclass(frozen=True)
class SourceLoad:
    """Per-source outcome for the run summary."""
    name: str
    present: bool                 # manifest file exists on disk
    count: int                    # entries this source contributed (after dedup)