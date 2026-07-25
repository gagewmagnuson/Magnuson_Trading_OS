"""CoverageEntry honest-date extension (backward-compatible)."""
from __future__ import annotations

from datetime import date

from trading_os.universe.coverage import load_manifest


def _write(tmp_path, text):
    p = tmp_path / "m.csv"
    p.write_text(text)
    return p


def test_manifest_without_date_columns_unchanged(tmp_path):
    """Existing manifests (no date columns) still load, dates None."""
    p = _write(tmp_path, "ticker,security_type,name\nAAPL,EQUITY,Apple Inc\n")
    entries = load_manifest(p)
    assert len(entries) == 1
    assert entries[0].ticker == "AAPL"
    assert entries[0].valid_from is None
    assert entries[0].valid_to is None


def test_manifest_with_honest_dates(tmp_path):
    p = _write(
        tmp_path,
        "ticker,security_type,name,valid_from,valid_to\n"
        "CELG,EQUITY,Celgene Corp,1986-01-01,2019-11-20\n"
        "AAPL,EQUITY,Apple Inc,1980-12-12,\n",
    )
    entries = {e.ticker: e for e in load_manifest(p)}
    assert entries["CELG"].valid_from == date(1986, 1, 1)
    assert entries["CELG"].valid_to == date(2019, 11, 20)
    assert entries["AAPL"].valid_from == date(1980, 12, 12)
    assert entries["AAPL"].valid_to is None      # blank -> open bound


def test_blank_date_cells_are_none(tmp_path):
    p = _write(tmp_path, "ticker,valid_from,valid_to\nXYZ,,\n")
    e = load_manifest(p)[0]
    assert e.valid_from is None and e.valid_to is None