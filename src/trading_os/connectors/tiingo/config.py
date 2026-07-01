"""Tiingo client configuration (mirrors the connector config style)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_URL = "https://api.tiingo.com"
DAILY_PRICES_PATH = "/tiingo/daily/{ticker}/prices"

# Tiingo free tier is generous for our low request count; pace politely.
REQUESTS_PER_MINUTE = 60


@dataclass(frozen=True)
class TiingoConfig:
    lake_root: Path = field(
        default_factory=lambda: Path(os.environ.get("TRADING_OS_LAKE", "lake"))
    )

    @property
    def bronze_dir(self) -> Path:
        return self.lake_root / "bronze" / "tiingo" / "daily"

    @property
    def request_interval(self) -> float:
        return 60.0 / REQUESTS_PER_MINUTE