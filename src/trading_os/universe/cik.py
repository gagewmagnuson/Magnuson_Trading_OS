"""
CIK resolution from SEC company_tickers.json.

One HTTP fetch (a single file covering every filer), built once into a
ticker -> zero-padded-10-digit-CIK map, then resolved locally for all tickers.
Uses the stdlib urllib with the SEC User-Agent convention (no third-party deps),
consistent with the EDGAR connector.

Repo path: src/trading_os/universe/cik.py
"""
from __future__ import annotations

import json
import urllib.request


class CikResolver:
    def __init__(self, config):
        self.config = config
        self._map: dict[str, str] | None = None

    def load(self) -> dict[str, str]:
        """Fetch + cache the ticker -> CIK map. Fetches once per instance."""
        if self._map is not None:
            return self._map
        req = urllib.request.Request(
            self.config.company_tickers_url,
            headers={"User-Agent": self.config.sec_user_agent},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
        # Shape: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
        m: dict[str, str] = {}
        for row in data.values():
            t = str(row.get("ticker", "")).strip().upper()
            cik = row.get("cik_str")
            if t and cik is not None:
                m.setdefault(t, str(cik).zfill(10))
        self._map = m
        return m

    def resolve(self, ticker: str) -> str | None:
        """
        Look up a CIK, tolerating the common share-class punctuation mismatch
        between holdings files and SEC (e.g. BRK.B vs BRK-B). Returns None when
        unresolved — expected for some share classes; CIK is nullable.
        """
        m = self.load()
        t = ticker.strip().upper()
        for cand in (t, t.replace(".", "-"), t.replace("-", "."), t.replace(".", "")):
            if cand in m:
                return m[cand]
        return None