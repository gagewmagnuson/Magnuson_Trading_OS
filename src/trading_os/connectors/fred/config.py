"""
FRED/ALFRED connector configuration.

The seeded series list (13 series) is the V0 macro vocabulary: one or two per
economic dimension the blueprint named (inflation, employment, rates, yield
curve, growth, housing, credit). Expanding it is a deliberate governance act,
like the EDGAR concept dictionary — add here and record the decision.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ALFRED observations endpoint. Returns vintage rows when a realtime range is
# supplied. We request the full sentinel range to get ALL vintages.
ALFRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES_URL = "https://api.stlouisfed.org/fred/series"

# Full-history realtime window. 1776-07-04 is ALFRED's documented earliest
# sentinel; 9999-12-31 means "through latest". Together they return every
# vintage of every observation.
REALTIME_START = "1776-07-04"
REALTIME_END = "9999-12-31"

# ALFRED's sentinel for "first/only known vintage" (predates revision tracking).
# Treated as a real first-known marker, not filtered out.
ALFRED_FIRST_VINTAGE_SENTINEL = "1776-07-04"

# Fair-use throttle. FRED allows generous rates; we stay polite.
REQUEST_DELAY_SECONDS = 0.4

# V0 macro series. (fred_id, human label, economic dimension, revisable).
#
# revisable=True  -> estimated statistics that get benchmark/seasonal revisions
#                    (GDP, CPI, payrolls...). Capture FULL ALFRED vintages.
# revisable=False -> market-observed series (Treasury yields, spreads). The
#                    value was known on the observation date and is never
#                    restated, so there is exactly ONE vintage: vintage_date =
#                    obs_date. These have no meaningful ALFRED revision history
#                    (DEC-015). Same schema, same as-of logic — just 1 vintage.
SERIES: list[tuple[str, str, str, bool]] = [
    ("CPIAUCSL",     "CPI, All Urban Consumers, SA",          "inflation",   True),
    ("PPIACO",       "PPI, All Commodities",                  "inflation",   True),
    ("UNRATE",       "Unemployment Rate",                     "employment",  True),
    ("PAYEMS",       "Total Nonfarm Payrolls",                "employment",  True),
    ("FEDFUNDS",     "Effective Federal Funds Rate (monthly)","rates",       True),
    ("DGS3MO",       "3-Month Treasury Yield",                "yield_curve", False),
    ("DGS2",         "2-Year Treasury Yield",                 "yield_curve", False),
    ("DGS10",        "10-Year Treasury Yield",                "yield_curve", False),
    ("T10Y2Y",       "10Y minus 2Y Treasury Spread",          "yield_curve", False),
    ("GDPC1",        "Real GDP",                              "growth",      True),
    ("HOUST",        "Housing Starts",                        "housing",     True),
    ("BAMLH0A0HYM2", "ICE BofA US High Yield OAS",            "credit",      True),
]

SERIES_IDS = [s[0] for s in SERIES]
REVISABLE = {s[0]: s[3] for s in SERIES}


@dataclass(frozen=True)
class FredConfig:
    lake_root: Path = field(
        default_factory=lambda: Path(os.environ.get("TRADING_OS_LAKE", "lake"))
    )
    request_delay: float = REQUEST_DELAY_SECONDS

    @property
    def bronze_dir(self) -> Path:
        return self.lake_root / "bronze" / "fred" / "observations"