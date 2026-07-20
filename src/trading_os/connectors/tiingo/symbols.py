"""
Tiingo ticker-format translation.

Repo path: src/trading_os/connectors/tiingo/symbols.py

The security master holds the canonical market ticker (e.g. 'BRK.B'). Tiingo
expects share-class tickers with a hyphen ('BRK-B'). That is a VENDOR CONVENTION,
so the translation lives in the Tiingo connector and never leaks into the
canonical identity layer — Bar.symbol and sec.security_identifier keep the
canonical form regardless of which vendor supplied the data.

Used by the bars connector, and by the actions connector whenever its history is
deepened (it fetches by ticker too, so it hits the same 404).
"""
from __future__ import annotations


def to_tiingo_symbol(symbol: str) -> str:
    """Canonical ticker -> Tiingo's ticker format.

    Tiingo uses '-' as the share-class separator where the canonical/exchange form
    uses '.': BRK.B -> BRK-B, BF.B -> BF-B. A no-op for ordinary tickers.
    """
    return symbol.replace(".", "-")