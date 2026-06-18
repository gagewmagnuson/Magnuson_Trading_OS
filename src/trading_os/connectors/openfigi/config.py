"""OpenFIGI connector configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

OPENFIGI_MAPPING_URL = "https://api.openfigi.com/v3/mapping"

# The 5-ticker validation set (matches the EDGAR cohort). For US equities we
# request exchCode 'US' to get the US composite mapping.
TICKERS: list[str] = ["AAPL", "MSFT", "GOOGL", "AMZN", "JPM"]

# Keyless rate limit is ~25 requests/min; we batch all tickers into one POST,
# so a single request covers the whole set. Delay is a courtesy buffer.
REQUEST_DELAY_SECONDS = 0.5

# US composite exchange code. OpenFIGI returns one FIGI per (ticker, exchange);
# 'US' yields the country-composite mapping we want as the primary anchor.
US_EXCH_CODE = "US"


@dataclass(frozen=True)
class OpenFigiConfig:
    lake_root: Path = field(
        default_factory=lambda: Path(os.environ.get("TRADING_OS_LAKE", "lake"))
    )
    request_delay: float = REQUEST_DELAY_SECONDS
    # Optional API key (None for keyless low-volume use).
    api_key: str | None = field(
        default_factory=lambda: os.environ.get("TRADING_OS_OPENFIGI_KEY") or None
    )

    @property
    def bronze_dir(self) -> Path:
        return self.lake_root / "bronze" / "openfigi" / "mapping"