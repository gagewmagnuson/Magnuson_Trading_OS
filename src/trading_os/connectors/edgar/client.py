"""
SEC Company Facts client. Single responsibility: download raw JSON to the
immutable bronze layer (DEC-012) and return a pointer. It does NOT parse.

Endpoint: https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import EdgarConfig
from .models import BronzeRef

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


class EdgarClient:
    def __init__(self, config: EdgarConfig):
        self.config = config
        self.config.bronze_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Host": "data.sec.gov",
            },
        )
        time.sleep(self.config.request_delay)  # fair-access throttle
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            # urllib does not auto-decompress; handle gzip if present.
            if resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
        return raw

    def fetch_company_facts(self, ticker: str, cik: str) -> BronzeRef:
        """
        Download Company Facts for one CIK and write it to bronze, immutably.

        Bronze filename embeds the download date so re-runs never overwrite a
        prior capture: companyfacts_<CIK>_<YYYYMMDD>.json. If a file for today
        already exists, it is reused (idempotent within a day) rather than
        re-downloaded.
        """
        url = COMPANYFACTS_URL.format(cik=cik)
        now = datetime.now(timezone.utc)
        fname = f"companyfacts_{cik}_{now:%Y%m%d}.json"
        path = self.config.bronze_dir / fname

        if path.exists():
            return BronzeRef(ticker=ticker, cik=cik, path=str(path), downloaded_at=now)

        try:
            raw = self._get(url)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"SEC HTTP {e.code} for {ticker} (CIK {cik}): {e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"SEC connection error for {ticker}: {e.reason}") from e

        # Validate it parses as JSON before we trust the bronze file.
        try:
            json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"SEC returned non-JSON for {ticker} (CIK {cik})") from e

        # Atomic write: temp then rename, so a crash never leaves a partial file.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(raw)
        tmp.replace(path)
        return BronzeRef(ticker=ticker, cik=cik, path=str(path), downloaded_at=now)