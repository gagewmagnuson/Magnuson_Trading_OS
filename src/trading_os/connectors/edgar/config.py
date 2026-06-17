"""
Connector configuration. The five-ticker scope (DEC-011) lives here as a hard
limit: the CLI refuses to run outside this set until the decision is amended.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --- DEC-011: validation cohort. Do NOT expand without amending DECISIONS.md. ---
# CIK is the SEC's zero-padded 10-digit Central Index Key used by the
# Company Facts API. Tickers are here only for human readability/logging.
VALIDATION_TICKERS: dict[str, str] = {
    "AAPL":  "0000320193",
    "MSFT":  "0000789019",
    "GOOGL": "0001652044",
    "AMZN":  "0001018724",
    "JPM":   "0000019617",
}

# SEC requires a descriptive User-Agent with contact info on all requests.
# Override via env var TRADING_OS_SEC_UA, e.g. "Magnuson Trading OS you@email.com".
DEFAULT_USER_AGENT = os.environ.get(
    "TRADING_OS_SEC_UA",
    "Magnuson Trading OS admin@example.com",
)

# SEC fair-access guidance is <= 10 requests/sec. We stay well under.
REQUEST_DELAY_SECONDS = 0.25

# Forms we accept fundamentals from in V0. 10-K = annual, 10-Q = quarterly.
ACCEPTED_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}

# Only us-gaap taxonomy in V0 (Company Facts also exposes 'dei', 'invest', etc.).
ACCEPTED_TAXONOMIES = {"us-gaap"}

# Relative tolerance for declaring two valid mapped tags "materially" in
# conflict (DEC-009 Rule #4). 0.5% guards against rounding/rescaling noise.
CONFLICT_REL_TOLERANCE = 0.005


@dataclass(frozen=True)
class EdgarConfig:
    # Postgres connection (psycopg3 conninfo string or libpq env vars).
    pg_conninfo: str = field(
        default_factory=lambda: os.environ.get("TRADING_OS_PG", "dbname=tradingos")
    )
    # Repo-root-relative lake path. Bronze JSON lands under here.
    lake_root: Path = field(
        default_factory=lambda: Path(os.environ.get("TRADING_OS_LAKE", "lake"))
    )
    user_agent: str = DEFAULT_USER_AGENT
    request_delay: float = REQUEST_DELAY_SECONDS

    @property
    def bronze_dir(self) -> Path:
        return self.lake_root / "bronze" / "edgar" / "companyfacts"