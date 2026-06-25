"""
Parse raw Alpaca daily-bar dicts into Bar rows, resolved to security_id.

An Alpaca 1Day bar: {"t": "2016-01-04T05:00:00Z", "o","h","l","c","v","n","vw"}.
For daily bars the timestamp's date is the session date. Symbols absent from the
security_id map are dropped here (the caller already counts them as skipped).
"""
from __future__ import annotations

from datetime import date

from .models import Bar


def parse_bars(
    raw_by_symbol: dict[str, list[dict]], sec_id_by_symbol: dict[str, int]
) -> list[Bar]:
    out: list[Bar] = []
    for sym, bars in raw_by_symbol.items():
        sec_id = sec_id_by_symbol.get(sym)
        if sec_id is None:
            continue
        for b in bars:
            ts = b.get("t")
            if not ts:
                continue
            out.append(
                Bar(
                    security_id=sec_id,
                    symbol=sym,
                    session_date=date.fromisoformat(ts[:10]),
                    open=float(b["o"]),
                    high=float(b["h"]),
                    low=float(b["l"]),
                    close=float(b["c"]),
                    volume=int(b["v"]),
                    trade_count=(int(b["n"]) if b.get("n") is not None else None),
                    vwap=(float(b["vw"]) if b.get("vw") is not None else None),
                )
            )
    return out