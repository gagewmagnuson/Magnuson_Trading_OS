"""Tiingo ticker-format translation (vendor convention, not canonical identity)."""
from trading_os.connectors.tiingo.symbols import to_tiingo_symbol


def test_share_class_dot_becomes_hyphen():
    assert to_tiingo_symbol("BRK.B") == "BRK-B"
    assert to_tiingo_symbol("BF.B") == "BF-B"


def test_ordinary_ticker_unchanged():
    assert to_tiingo_symbol("AAPL") == "AAPL"
    assert to_tiingo_symbol("MSFT") == "MSFT"