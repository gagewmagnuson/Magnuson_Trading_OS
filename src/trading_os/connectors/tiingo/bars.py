"""
Parse a Tiingo daily-price series into canonical Bars (DEC-004, DEC-024).

Repo path: src/trading_os/connectors/tiingo/bars.py

Tiingo's daily endpoint returns one row per session with BOTH raw and adjusted
fields. Per DEC-004 we store the RAW, unadjusted OHLCV (open/high/low/close/
volume) and let the CAF engine adjust on read — verified empirically: AAPL raw
close is ~$500 before the 2020-08-31 4:1 split and ~$129 after, i.e. genuinely
unadjusted (adjClose is the split-adjusted reference we deliberately do NOT store).

Tiingo has no per-bar trade_count or vwap, so those map to None on the canonical
Bar (rendered as null downstream). Mirrors the conventions of the sibling Tiingo
actions parser (tiingo/actions.py): row.get(...) access, `or default` null guards,
date.fromisoformat(row["date"][:10]) for the session date.

Per-security (the Tiingo endpoint is per-ticker), unlike Alpaca's multi-symbol map.
"""
from __future__ import annotations

from datetime import date

from trading_os.bars.models import Bar

from dataclasses import dataclass


@dataclass(frozen=True)
class ParseAnomaly:
    """A Tiingo row excluded during parse, with the reason (DQ anomaly).
    Mirrors bars.writer.SkippedBar; the CLI reports both together."""
    symbol: str
    session_date: str | None   # ISO date string if known, else None
    reason: str

def _row_date(row: dict) -> date:
    # Tiingo date is e.g. "2020-08-31T00:00:00.000Z"
    return date.fromisoformat(row["date"][:10])


def parse_bars(
    security_id: int, symbol: str, rows: list[dict]
) -> tuple[list[Bar], list[ParseAnomaly]]:
    """Build canonical Bars from one ticker's Tiingo daily series (raw OHLCV).

    Returns (bars, anomalies). A row missing its date or carrying non-numeric /
    missing OHLC is skipped as a structured ParseAnomaly, not raised — one bad
    vendor row never fails a multi-security backfill (same graceful-boundary
    stance as the writer's non-session skip). Volume nulls coerce to 0.
    """
    out: list[Bar] = []
    anomalies: list[ParseAnomaly] = []
    for row in rows:
        raw_date = row.get("date")
        if not raw_date:
            anomalies.append(ParseAnomaly(symbol, None, "missing_date"))
            continue
        try:
            sd = date.fromisoformat(raw_date[:10])
            bar = Bar(
                security_id=security_id,
                symbol=symbol,
                session_date=sd,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row.get("volume", 0) or 0),
                trade_count=None,
                vwap=None,
            )
        except (KeyError, TypeError, ValueError) as e:
            anomalies.append(ParseAnomaly(symbol, raw_date[:10], f"malformed_row:{type(e).__name__}"))
            continue
        out.append(bar)
    return out, anomalies