"""OpenFIGI connector configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

OPENFIGI_MAPPING_URL = "https://api.openfigi.com/v3/mapping"

# US composite exchange code. OpenFIGI returns one FIGI per (ticker, exchange);
# 'US' yields the country-composite mapping we want as the primary anchor.
US_EXCH_CODE = "US"

# OpenFIGI mapping limits, auto-selected by whether an API key is present.
# These are VENDOR limits and change periodically — verify against current
# OpenFIGI docs (https://www.openfigi.com/api#rate-limit) and adjust here; no
# other code change is needed.
KEYLESS_MAX_JOBS_PER_REQUEST = 10
KEYED_MAX_JOBS_PER_REQUEST = 100
KEYLESS_REQUESTS_PER_MINUTE = 25
KEYED_REQUESTS_PER_MINUTE = 250

# Safety margin on the computed inter-request interval (1.0 = none).
RATE_SAFETY_FACTOR = 1.10


@dataclass(frozen=True)
class OpenFigiConfig:
    lake_root: Path = field(
        default_factory=lambda: Path(os.environ.get("TRADING_OS_LAKE", "lake"))
    )
    # Optional API key (None for keyless low-volume use). Presence selects the
    # keyed limits below.
    api_key: str | None = field(
        default_factory=lambda: os.environ.get("TRADING_OS_OPENFIGI_KEY") or None
    )

    @property
    def bronze_dir(self) -> Path:
        return self.lake_root / "bronze" / "openfigi" / "mapping"

    @property
    def max_jobs_per_request(self) -> int:
        return (KEYED_MAX_JOBS_PER_REQUEST if self.api_key
                else KEYLESS_MAX_JOBS_PER_REQUEST)

    @property
    def requests_per_minute(self) -> int:
        return (KEYED_REQUESTS_PER_MINUTE if self.api_key
                else KEYLESS_REQUESTS_PER_MINUTE)

    @property
    def request_interval(self) -> float:
        """Seconds to wait between requests to stay under the per-minute limit."""
        return (60.0 / self.requests_per_minute) * RATE_SAFETY_FACTOR