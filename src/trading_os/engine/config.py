"""
Engine configuration.

Repo path: src/trading_os/engine/config.py
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Heavily-revised real GDP -> many ALFRED vintages. Used to prove that a vintage
# revision is seen correctly THROUGH the Parquet -> DuckDB -> Postgres path.
VINTAGE_DEMO_SERIES = "GDPC1"

# Contemporaneous market series attached as macro context next to the
# per-security fundamentals snapshot (single-vintage, DEC-015).
CONTEXT_SERIES = "DGS10"  # 10-Year Treasury yield

# The as-of date for the capstone "as-known world state" query.
DEFAULT_AS_OF = date(2020, 6, 30)


@dataclass(frozen=True)
class EngineConfig:
    lake_root: Path = field(
        default_factory=lambda: Path(os.environ.get("TRADING_OS_LAKE", "lake"))
    )
    as_of: date = DEFAULT_AS_OF

    @property
    def macro_silver_dir(self) -> Path:
        # Derived, regenerable, NON-authoritative V0 proof artifact (gitignored).
        # NOT a system of record — Postgres is. See store.py module docstring.
        return self.lake_root / "silver" / "macro_obs"
    
    @property
    def bars_eod_dir(self) -> Path:
        # Authoritative silver: unadjusted EOD bars (DEC-003). Generic path; the
        # engine reads it vendor-agnostically. Unlike macro_silver_dir this is a
        # real system-of-record dataset, not a derived proof artifact.
        return self.lake_root / "silver" / "bars_eod"