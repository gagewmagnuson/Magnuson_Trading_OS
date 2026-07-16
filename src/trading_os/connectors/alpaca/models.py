"""Typed shapes for the Alpaca bars connector.

The canonical `Bar` now lives in trading_os.bars.models (DEC-024: dataset-scoped,
source-independent). It is re-exported here so intra-connector imports remain
stable; BronzeRef is Alpaca-specific and stays.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_os.bars.models import Bar  # re-export: canonical bars model

__all__ = ["Bar", "BronzeRef"]


@dataclass(frozen=True)
class BronzeRef:
    path: str
    downloaded_at: datetime