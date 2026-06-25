"""Alpaca EOD bars connector configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Alpaca market-data REST.
DATA_BASE_URL = "https://data.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"

# DEC-004: store UNADJUSTED prices, adjust on read. Always request raw.
ADJUSTMENT = "raw"

# Basic plan serves the consolidated tape (CTA/UTP) for data older than 15 min.
# 'sip' = full consolidated; 'iex' is the fallback if an account rejects 'sip'.
DEFAULT_FEED = "sip"

TIMEFRAME = "1Day"

# Alpaca's earliest equity history (Basic plan: since 2016).
HISTORY_START = date(2016, 1, 1)

# Basic plan: 200 requests/min. Multi-symbol pages keep us well under it.
REQUESTS_PER_MINUTE = 200
SYMBOLS_PER_REQUEST = 100      # Alpaca accepts many symbols per request
PAGE_LIMIT = 10000            # max bars per page (Alpaca cap)


@dataclass(frozen=True)
class AlpacaConfig:
    lake_root: Path = field(
        default_factory=lambda: Path(os.environ.get("TRADING_OS_LAKE", "lake"))
    )
    feed: str = DEFAULT_FEED
    adjustment: str = ADJUSTMENT
    timeframe: str = TIMEFRAME
    start: date = HISTORY_START
    symbols_per_request: int = SYMBOLS_PER_REQUEST
    page_limit: int = PAGE_LIMIT

    @property
    def bronze_dir(self) -> Path:
        return self.lake_root / "bronze" / "alpaca" / "bars_eod"

    @property
    def silver_dir(self) -> Path:
        return self.lake_root / "silver" / "bars_eod"

    @property
    def request_interval(self) -> float:
        return 60.0 / REQUESTS_PER_MINUTE