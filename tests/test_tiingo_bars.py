"""Tests for the Tiingo bars parser (raw OHLCV -> canonical Bar, DEC-004)."""
from __future__ import annotations

from datetime import date

from trading_os.connectors.tiingo.bars import parse_bars


def _row(d, o=500.04, h=505.0, l=495.0, c=499.23, v=1000000):
    return {"date": f"{d}T00:00:00.000Z", "open": o, "high": h, "low": l,
            "close": c, "volume": v, "adjClose": 121.07, "splitFactor": 1.0,
            "divCash": 0.0}


def test_parses_raw_ohlcv():
    bars, anomalies = parse_bars(1, "AAPL", [_row("2020-08-27")])
    assert anomalies == []
    b = bars[0]
    assert b.security_id == 1 and b.symbol == "AAPL"
    assert b.session_date == date(2020, 8, 27)
    assert b.close == 499.23           # RAW close, not adjClose (121.07)
    assert b.trade_count is None and b.vwap is None


def test_stores_raw_not_adjusted_across_split():
    rows = [_row("2020-08-28", c=499.23), _row("2020-08-31", c=129.04)]
    bars, _ = parse_bars(1, "AAPL", rows)
    assert bars[0].close == 499.23
    assert bars[1].close == 129.04


def test_missing_date_is_anomaly_not_crash():
    bars, anomalies = parse_bars(1, "AAPL", [{"open": 1, "high": 1, "low": 1,
                                              "close": 1, "volume": 1}])
    assert bars == []
    assert len(anomalies) == 1 and anomalies[0].reason == "missing_date"


def test_malformed_ohlc_is_anomaly_not_crash():
    """A row with a non-numeric close is skipped as an anomaly, not raised —
    one bad vendor row must not fail the whole backfill."""
    bad = _row("2020-08-27"); bad["close"] = "N/A"
    bars, anomalies = parse_bars(1, "AAPL", [bad])
    assert bars == []
    assert len(anomalies) == 1 and anomalies[0].reason.startswith("malformed_row")
    assert anomalies[0].session_date == "2020-08-27"


def test_null_volume_coerced_to_zero():
    row = _row("2020-08-27"); row["volume"] = None
    bars, _ = parse_bars(1, "AAPL", [row])
    assert bars[0].volume == 0