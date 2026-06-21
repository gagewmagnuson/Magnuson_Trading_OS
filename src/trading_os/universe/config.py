"""
Universe layer configuration.

Coverage sources are declared in manifests/registry.csv (not hardcoded here);
this file only locates the registry + manifest directory and holds the SEC
fetch settings. Adding a coverage set = a registry row + a manifest, no code change.

Repo path: src/trading_os/universe/config.py
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
MANIFEST_DIR = _PKG_DIR / "manifests"
REGISTRY_PATH = MANIFEST_DIR / "registry.csv"

# SEC publishes ticker -> CIK for every filer here (a single file).
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# SEC requires a descriptive User-Agent with contact info on all requests.
# Same convention as the EDGAR connector; override via TRADING_OS_SEC_UA.
SEC_USER_AGENT = os.environ.get(
    "TRADING_OS_SEC_UA", "Magnuson Trading OS admin@example.com"
)

# ref.data_source name for identities minted by this layer.
UNIVERSE_SOURCE_NAME = "UNIVERSE"


@dataclass(frozen=True)
class UniverseConfig:
    manifest_dir: Path = MANIFEST_DIR
    registry_path: Path = REGISTRY_PATH
    company_tickers_url: str = COMPANY_TICKERS_URL
    sec_user_agent: str = SEC_USER_AGENT