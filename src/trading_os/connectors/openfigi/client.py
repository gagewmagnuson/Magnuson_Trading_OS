"""
OpenFIGI mapping client. POSTs a batch of ticker queries, caches the raw JSON
response to immutable bronze (DEC-012). Reads optional API key from config;
works keyless for low volume.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .config import US_EXCH_CODE, OpenFigiConfig
from .models import BronzeRef


class OpenFigiClient:
    def __init__(self, config: OpenFigiConfig):
        self.config = config
        self.config.bronze_dir.mkdir(parents=True, exist_ok=True)

    def map_tickers(self, tickers: list[str]) -> tuple[BronzeRef, list[dict]]:
        """
        POST one mapping request for all tickers. Returns the bronze ref and the
        parsed top-level list (one entry per query, in request order).

        OpenFIGI mapping request body is a list of query objects:
          [{"idType":"TICKER","idValue":"AAPL","exchCode":"US"}, ...]
        Response is a parallel list: [{"data":[...]} | {"warning":"..."}].
        """
        now = datetime.now(timezone.utc)
        fname = f"mapping_{now:%Y%m%d_%H%M%S}.json"
        path = self.config.bronze_dir / fname

        body = [
            {"idType": "TICKER", "idValue": t, "exchCode": US_EXCH_CODE}
            for t in tickers
        ]
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.config.api_key

        req = urllib.request.Request(
            "https://api.openfigi.com/v3/mapping", data=payload,
            headers=headers, method="POST",
        )
        time.sleep(self.config.request_delay)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"OpenFIGI HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"OpenFIGI connection error: {e.reason}") from e

        parsed = json.loads(raw)
        # Immutable bronze: store the request alongside the response so the
        # ticker->entry order is reconstructable.
        bronze_doc = {"request": body, "response": parsed}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(json.dumps(bronze_doc).encode("utf-8"))
        tmp.replace(path)
        return BronzeRef(path=str(path), downloaded_at=now), parsed