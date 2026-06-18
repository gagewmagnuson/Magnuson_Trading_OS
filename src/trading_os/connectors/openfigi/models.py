"""Typed shapes for the OpenFIGI connector."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BronzeRef:
    path: str
    downloaded_at: datetime


@dataclass(frozen=True)
class FigiIdentity:
    """The resolved identity for one ticker, ready to enrich sec.security."""
    ticker: str
    figi: str                  # composite FIGI (primary anchor)
    share_class_figi: str | None
    name: str | None
    security_type: str | None  # OpenFIGI securityType (e.g. 'Common Stock')
    market_sector: str | None  # e.g. 'Equity'
    exch_code: str | None