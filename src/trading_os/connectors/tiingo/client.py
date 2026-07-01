"""
Tiingo daily-price client. One endpoint per ticker returns the full daily series
with raw OHLCV, adjClose (CRSP-style reference), and inline actions (divCash,
splitFactor). Stdlib urllib only; raw responses cached to immutable bronze
(DEC-012). Mirrors the Alpaca client structure.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from trading_os.config import settings

from .config import BASE_URL, DAILY_PRICES_PATH, TiingoConfig


class TiingoClient:
    def __init__(self, config: TiingoConfig | None = None):
        self.config = config or TiingoConfig()
        self.config.bronze_dir.mkdir(parents=True, exist_ok=True)
        self.token = settings.tiingo_key()

    def fetch_daily(
        self, ticker: str, start: date, end: date | None = None
    ) -> list[dict]:
        """
        Full daily series for `ticker` from `start`. Each row:
          {date, open, high, low, close, volume, adjOpen, adjHigh, adjLow,
           adjClose, adjVolume, divCash, splitFactor}
        Raw response cached to bronze. Returns the parsed JSON list.
        """
        now = datetime.now(timezone.utc)
        params = {
            "startDate": start.isoformat(),
            "token": self.token,
            "format": "json",
            "resampleFreq": "daily",
        }
        if end:
            params["endDate"] = end.isoformat()
        path_part = DAILY_PRICES_PATH.format(ticker=ticker.lower())
        rows = self._get(path_part, params)

        # Bronze WITHOUT the token in the cached URL/params.
        safe_params = {k: v for k, v in params.items() if k != "token"}
        out = self.config.bronze_dir / f"{ticker.upper()}_{now:%Y%m%d_%H%M%S}.json"
        doc = {"fetched_at": now.isoformat(), "ticker": ticker.upper(),
               "params": safe_params, "rows": rows}
        tmp = out.with_suffix(".json.tmp")
        tmp.write_bytes(json.dumps(doc).encode("utf-8"))
        tmp.replace(out)
        return rows

    def _get(self, path_part: str, params: dict, _attempt: int = 1):
        url = f"{BASE_URL}{path_part}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and _attempt <= 4:
                time.sleep(self.config.request_interval * (2 ** _attempt))
                return self._get(path_part, params, _attempt + 1)
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Tiingo HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Tiingo connection error: {e.reason}") from e