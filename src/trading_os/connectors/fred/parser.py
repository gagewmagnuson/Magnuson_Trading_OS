"""
Parse immutable bronze ALFRED files into typed VintageObs. Reads disk only
(DEC-012); never the network.

ALFRED observations JSON shape:
  {
    "realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
    "observation_start": "...", "observation_end": "...",
    "units": "lin", "output_type": 4, "count": N,
    "observations": [
      {"realtime_start":"2018-01-26","realtime_end":"2018-02-27",
       "date":"2017-10-01","value":"4849.964"},
      ...
    ]
  }

Each entry's realtime_start is the vintage_date (knowledge_time). Missing
values arrive as ".", parsed to None. The 1776-07-04 sentinel realtime_start
marks a first/only vintage and is preserved as-is (a real first-known date),
NOT discarded.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterator

from .models import BronzeRef, VintageObs


def _d(s: str | None) -> date | None:
    if not s:
        return None
    return date.fromisoformat(s)


def _num(s: str | None) -> float | None:
    # ALFRED uses "." for missing observations.
    if s is None or s == "." or s.strip() == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_bronze(ref: BronzeRef) -> Iterator[VintageObs]:
    doc = json.loads(Path(ref.path).read_text())
    # Market-rate series (DEC-015) are single-vintage: the value was known on
    # the observation date, so vintage_date = obs_date and realtime_end is open.
    single_vintage = bool(doc.get("_trading_os_single_vintage", False))
    for o in doc.get("observations", []):
        odate = o.get("date")
        if not odate:
            continue
        obs_d = _d(odate)
        if single_vintage:
            vintage_d = obs_d            # knowledge_time = event_time
            rend = None
        else:
            rstart = o.get("realtime_start")
            if not rstart:
                continue
            vintage_d = _d(rstart)       # knowledge_time = ALFRED realtime_start
            rend = _d(o.get("realtime_end"))
        yield VintageObs(
            series_id=ref.series_id,
            obs_date=obs_d,
            vintage_date=vintage_d,
            realtime_end=rend,
            value=_num(o.get("value")),
        )