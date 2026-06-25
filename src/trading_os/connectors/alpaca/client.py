"""
Alpaca daily-bar client. Fetches multi-symbol 1Day bars (raw/unadjusted,
consolidated feed) with page-token pagination, paced under the Basic-plan
200 req/min limit, and caches the raw pages to immutable bronze (DEC-012).
Stdlib urllib only — no third-party HTTP dependency.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from trading_os.config import settings

from .config import BARS_PATH, DATA_BASE_URL, AlpacaConfig
from .models import BronzeRef


class AlpacaClient:
    def __init__(self, config: AlpacaConfig):
        self.config = config
        self.config.bronze_dir.mkdir(parents=True, exist_ok=True)
        self.key = settings.alpaca_key()
        self.secret = settings.alpaca_secret()

    def fetch_daily_bars(
        self, symbols: list[str], start: date, end: date | None = None
    ) -> tuple[BronzeRef, dict[str, list[dict]]]:
        """
        Fetch daily bars for `symbols` from `start`. Returns the bronze ref and
        a {symbol: [raw bar dict, ...]} map. Symbols are chunked per request and
        each chunk is paginated to exhaustion via next_page_token.
        """
        now = datetime.now(timezone.utc)
        all_bars: dict[str, list[dict]] = {s: [] for s in symbols}
        pages: list[dict] = []

        per = self.config.symbols_per_request
        for i in range(0, len(symbols), per):
            chunk = symbols[i:i + per]
            page_token: str | None = None
            while True:
                params = {
                    "symbols": ",".join(chunk),
                    "timeframe": self.config.timeframe,
                    "start": start.isoformat(),
                    "adjustment": self.config.adjustment,
                    "feed": self.config.feed,
                    "limit": str(self.config.page_limit),
                    "sort": "asc",
                }
                if end:
                    params["end"] = end.isoformat()
                if page_token:
                    params["page_token"] = page_token
                data = self._get(params)
                pages.append(data)
                for sym, bars in (data.get("bars") or {}).items():
                    all_bars.setdefault(sym, []).extend(bars)
                page_token = data.get("next_page_token")
                if not page_token:
                    break
                time.sleep(self.config.request_interval)
            time.sleep(self.config.request_interval)

        path = self.config.bronze_dir / f"bars_eod_{now:%Y%m%d_%H%M%S}.json"
        doc = {
            "fetched_at": now.isoformat(),
            "symbols": symbols,
            "start": start.isoformat(),
            "end": end.isoformat() if end else None,
            "feed": self.config.feed,
            "adjustment": self.config.adjustment,
            "pages": pages,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(json.dumps(doc).encode("utf-8"))
        tmp.replace(path)
        return BronzeRef(path=str(path), downloaded_at=now), all_bars

    def _get(self, params: dict, _attempt: int = 1) -> dict:
        url = f"{DATA_BASE_URL}{BARS_PATH}?" + urllib.parse.urlencode(params, safe=",")
        req = urllib.request.Request(
            url,
            headers={
                "APCA-API-KEY-ID": self.key,
                "APCA-API-SECRET-KEY": self.secret,
                "accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and _attempt <= 4:
                time.sleep(self.config.request_interval * (2 ** _attempt))
                return self._get(params, _attempt + 1)
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Alpaca HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Alpaca connection error: {e.reason}") from e