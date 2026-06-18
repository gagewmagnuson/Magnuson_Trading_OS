"""
Parse the OpenFIGI mapping response into FigiIdentity records.

OpenFIGI returns, per query, a "data" list of candidate FIGIs. For a US-composite
query each candidate has fields like:
  {"figi":"BBG000B9XRY4","name":"APPLE INC","ticker":"AAPL",
   "exchCode":"US","compositeFIGI":"BBG000B9XRY4","securityType":"Common Stock",
   "marketSector":"Equity","shareClassFIGI":"BBG001S5N8V8","securityType2":"..."}

Identity selection (the one real judgment call here, analogous to EDGAR tag
choice): we anchor on the COMPOSITE FIGI. With exchCode='US' the candidate's
figi IS the US composite, so we prefer the entry whose figi == compositeFIGI.
If none match exactly, we fall back to the first candidate and flag nothing —
the writer records what it got; review can catch anomalies.
"""
from __future__ import annotations

from .models import FigiIdentity


def parse_identities(tickers: list[str], response: list[dict]) -> dict[str, FigiIdentity]:
    """
    Map each requested ticker (by request order) to a FigiIdentity, or omit it
    if OpenFIGI returned a warning / no data for that ticker.
    """
    out: dict[str, FigiIdentity] = {}
    for ticker, entry in zip(tickers, response):
        data = entry.get("data")
        if not data:
            # entry may be {"warning": "..."} — no mapping found; skip (logged
            # by the CLI). Missing beats wrong: we do not guess an identity.
            continue
        chosen = _select_composite(data)
        out[ticker] = FigiIdentity(
            ticker=ticker,
            figi=chosen.get("compositeFIGI") or chosen.get("figi"),
            share_class_figi=chosen.get("shareClassFIGI"),
            name=chosen.get("name"),
            security_type=chosen.get("securityType"),
            market_sector=chosen.get("marketSector"),
            exch_code=chosen.get("exchCode"),
        )
    return out


def _select_composite(data: list[dict]) -> dict:
    """Prefer the candidate that IS its own composite (figi == compositeFIGI)."""
    for d in data:
        if d.get("figi") and d.get("figi") == d.get("compositeFIGI"):
            return d
    return data[0]