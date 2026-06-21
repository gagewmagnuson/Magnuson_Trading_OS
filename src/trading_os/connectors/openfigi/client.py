"""
OpenFIGI mapping client. POSTs ticker queries in chunks sized to the per-request
job cap, paced to the per-minute limit (both from config, auto-selected by API
key), and caches the combined raw JSON to immutable bronze (DEC-012). Works
keyless for low volume; uses the API key when present.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .config import OPENFIGI_MAPPING_URL, US_EXCH_CODE, OpenFigiConfig
from .models import BronzeRef


class OpenFigiClient:
    def __init__(self, config: OpenFigiConfig):
        self.config = config
        self.config.bronze_dir.mkdir(parents=True, exist_ok=True)

    def map_tickers(self, tickers: list[str]) -> tuple[BronzeRef, list[dict]]:
        """
        Map all tickers, chunked to the per-request job cap and paced to the
        per-minute limit. Returns the bronze ref and the COMBINED response list,
        in request order so it stays positionally parallel to `tickers` for
        parse_identities.
        """
        now = datetime.now(timezone.utc)
        path = self.config.bronze_dir / f"mapping_{now:%Y%m%d_%H%M%S}.json"

        chunk_size = self.config.max_jobs_per_request
        full_request: list[dict] = []
        full_response: list[dict] = []

        for i in range(0, len(tickers), chunk_size):
            if i > 0:
                time.sleep(self.config.request_interval)
            chunk = tickers[i:i + chunk_size]
            body = [
                {"idType": "TICKER", "idValue": t, "exchCode": US_EXCH_CODE}
                for t in chunk
            ]
            parsed = self._post(body)
            if len(parsed) != len(chunk):
                # OpenFIGI returns exactly one entry per query, in order. A
                # length mismatch would desync the ticker->entry alignment that
                # parse_identities relies on, so fail loudly rather than guess.
                raise RuntimeError(
                    f"OpenFIGI returned {len(parsed)} entries for a "
                    f"{len(chunk)}-job request; refusing to misalign identities."
                )
            full_request.extend(body)
            full_response.extend(parsed)

        bronze_doc = {"request": full_request, "response": full_response}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(json.dumps(bronze_doc).encode("utf-8"))
        tmp.replace(path)
        return BronzeRef(path=str(path), downloaded_at=now), full_response

    def _post(self, body: list[dict], _attempt: int = 1) -> list[dict]:
        """POST one chunk; basic exponential backoff on HTTP 429."""
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.config.api_key
        req = urllib.request.Request(
            OPENFIGI_MAPPING_URL, data=payload, headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and _attempt <= 3:
                time.sleep(self.config.request_interval * (2 ** _attempt))
                return self._post(body, _attempt + 1)
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"OpenFIGI HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"OpenFIGI connection error: {e.reason}") from e