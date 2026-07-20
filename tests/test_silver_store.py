"""Tests for generic silver-deployment infrastructure (DEC-026)."""
from __future__ import annotations

from pathlib import Path

import pytest

from trading_os.bars.silver_store import (
    staging_dir, clear_staging, swap_staging_into_live,
)


def test_staging_dir_naming(tmp_path):
    live = tmp_path / "bars_eod"
    assert staging_dir(live).name == "bars_eod_staging"


def test_clear_staging_is_empty(tmp_path):
    live = tmp_path / "bars_eod"
    st = clear_staging(live)
    (st / "old.parquet").write_text("x")
    st2 = clear_staging(live)                # second call clears it
    assert list(st2.glob("*.parquet")) == []


def test_swap_moves_staging_to_live_and_backs_up(tmp_path):
    live = tmp_path / "bars_eod"
    live.mkdir(parents=True)
    (live / "old.parquet").write_text("old")
    st = staging_dir(live); st.mkdir(parents=True)
    (st / "new.parquet").write_text("new")

    backup = swap_staging_into_live(live)

    assert (live / "new.parquet").exists()
    assert not (live / "old.parquet").exists()
    assert (backup / "old.parquet").exists()
    assert not st.exists()


def test_swap_without_staging_raises(tmp_path):
    live = tmp_path / "bars_eod"; live.mkdir(parents=True)
    with pytest.raises(RuntimeError):
        swap_staging_into_live(live)


def test_swap_into_empty_lake_no_backup(tmp_path):
    """If there's no live silver yet (fresh system), staging just becomes live."""
    live = tmp_path / "bars_eod"
    st = staging_dir(live); st.mkdir(parents=True)
    (st / "new.parquet").write_text("new")
    swap_staging_into_live(live)
    assert (live / "new.parquet").exists()