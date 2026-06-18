"""
ALFRED client. Downloads full-vintage observations + series metadata to the
immutable bronze layer (DEC-012). Reads the API key via the shared settings
helper, never directly from os.environ.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from trading_os.config import settings

from datetime import date

from .config import (ALFRED_OBS_URL, FRED_SERIES_URL, REALTIME_END,
                     REALTIME_START, REVISABLE, FredConfig)
from .models import BronzeRef


class FredClient:
    def __init__(self, config: FredConfig):
        self.config = config
        self.config.bronze_dir.mkdir(parents=True, exist_ok=True)
        self._key = settings.fred_api_key()  # raises clear error if missing

    def _get_json(self, url: str, params: dict) -> dict:
        params = {**params, "api_key": self._key, "file_type": "json"}
        full = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full, headers={"User-Agent": "Magnuson Trading OS"})
        time.sleep(self.config.request_delay)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"FRED HTTP {e.code} for {url}: {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"FRED connection error: {e.reason}") from e
        return json.loads(raw)

    def fetch_series_meta(self, series_id: str) -> dict:
        """Series-level metadata (title, units, frequency, seasonal adj)."""
        return self._get_json(FRED_SERIES_URL, {"series_id": series_id})

    def fetch_observations(self, series_id: str) -> list[BronzeRef]:
        """
        Download a series into immutable bronze, returning one or more
        BronzeRefs.

        Two natures, ONE data model (DEC-015):
          * Non-revisable market series (Treasury yields/spreads): fetched from
            plain FRED (no realtime params). The value was known on the
            observation date, so there is exactly one vintage. The parser
            stamps vintage_date = obs_date.
          * Revisable statistics (GDP, CPI, payrolls...): fetched from ALFRED
            with the full realtime range to capture EVERY vintage. ALFRED caps
            a single request at 2000 vintage dates, so on that error we fall
            back to fixed realtime windows, one bronze file per window.
        """
        now = datetime.now(timezone.utc)

        if not REVISABLE.get(series_id, True):
            return self._fetch_latest_only(series_id, now)

        # --- revisable: full ALFRED vintages ---
        # Fast path: try the whole range in one request.
        single_path = self.config.bronze_dir / f"obs_{series_id}_{now:%Y%m%d}.json"
        if single_path.exists():
            return [BronzeRef(series_id=series_id, path=str(single_path), downloaded_at=now)]
        try:
            data = self._get_json(ALFRED_OBS_URL, {
                "series_id": series_id,
                "realtime_start": REALTIME_START,
                "realtime_end": REALTIME_END,
            })
            tmp = single_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(single_path)
            return [BronzeRef(series_id=series_id, path=str(single_path), downloaded_at=now)]
        except RuntimeError as e:
            if "maximum number of vintage dates" not in str(e):
                raise  # a different error: do not swallow it

        # Windowed fallback: chunk the realtime range by fixed spans. 4-year
        # windows keep even daily series under 2000 vintages with margin.
        refs: list[BronzeRef] = []
        windows = self._realtime_windows(start_year=1990, window_years=4)
        for i, (ws, we) in enumerate(windows):
            chunk_path = self.config.bronze_dir / f"obs_{series_id}_{now:%Y%m%d}_w{i:02d}.json"
            if chunk_path.exists():
                refs.append(BronzeRef(series_id=series_id, path=str(chunk_path), downloaded_at=now))
                continue
            data = self._get_json(ALFRED_OBS_URL, {
                "series_id": series_id,
                "realtime_start": ws,
                "realtime_end": we,
            })
            # Skip empty windows (no vintages in range) but still record file.
            tmp = chunk_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(chunk_path)
            refs.append(BronzeRef(series_id=series_id, path=str(chunk_path), downloaded_at=now))
        return refs

    def _fetch_latest_only(self, series_id: str, now) -> list[BronzeRef]:
        """Plain FRED latest-values fetch for non-revisable market series.
        No realtime params => one observation per date. Bronze is tagged so the
        parser knows to set vintage_date = obs_date."""
        path = self.config.bronze_dir / f"obs_{series_id}_{now:%Y%m%d}_latest.json"
        if path.exists():
            return [BronzeRef(series_id=series_id, path=str(path), downloaded_at=now)]
        data = self._get_json(ALFRED_OBS_URL, {"series_id": series_id})
        # Mark the payload so the parser treats it as single-vintage.
        data["_trading_os_single_vintage"] = True
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
        return [BronzeRef(series_id=series_id, path=str(path), downloaded_at=now)]

    @staticmethod
    def _realtime_windows(start_year: int, window_years: int) -> list[tuple[str, str]]:
        """Contiguous [start,end] realtime windows from start_year to today."""
        today = date.today()
        out: list[tuple[str, str]] = []
        y = start_year
        while y <= today.year:
            ws = date(y, 1, 1)
            we = date(min(y + window_years - 1, today.year), 12, 31)
            out.append((ws.isoformat(), we.isoformat()))
            y += window_years
        return out