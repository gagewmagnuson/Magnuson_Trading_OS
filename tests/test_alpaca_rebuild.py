"""
Tests for the Alpaca silver rebuild (DEC-024).

Hermetic: the correctness hinge (replay + latest-fetch-wins dedup) is a pure
function tested with synthetic bronze maps and a fake sec map — no DB, no network.
Plus bronze-doc loading order and the swap/rollback file ops.
"""
from __future__ import annotations

import json
from datetime import date

from trading_os.connectors.alpaca.config import AlpacaConfig
from trading_os.connectors.alpaca.rebuild import (
    load_bronze_docs,
    reassemble,
    replay_and_dedup,
    swap_staging_into_live,
    _staging_dir,
)


def _raw(t, o=1.0, h=2.0, l=0.5, c=1.5, v=1000):
    return {"t": f"{t}T05:00:00Z", "o": o, "h": h, "l": l, "c": c, "v": v,
            "n": 10, "vw": 1.4}


def test_reassemble_merges_pages():
    doc = {"pages": [
        {"bars": {"AAPL": [_raw("2020-01-02")]}},
        {"bars": {"AAPL": [_raw("2020-01-03")], "MSFT": [_raw("2020-01-02")]}},
    ]}
    m = reassemble(doc)
    assert len(m["AAPL"]) == 2
    assert len(m["MSFT"]) == 1


def test_load_bronze_docs_sorted_by_fetched_at(tmp_path):
    (tmp_path / "bars_eod_b.json").write_text(json.dumps({"fetched_at": "2026-02-01", "pages": []}))
    (tmp_path / "bars_eod_a.json").write_text(json.dumps({"fetched_at": "2026-01-01", "pages": []}))
    docs = load_bronze_docs(tmp_path)
    assert [d["fetched_at"] for d in docs] == ["2026-01-01", "2026-02-01"]


def test_replay_dedup_latest_fetch_wins():
    """Same (security, session) in two fetches: the LATER fetch's value wins."""
    sec_map = {"AAPL": 1}
    early = reassemble({"pages": [{"bars": {"AAPL": [_raw("2020-01-02", c=1.50)]}}]})
    late = reassemble({"pages": [{"bars": {"AAPL": [_raw("2020-01-02", c=9.99)]}}]})
    # ordered oldest-first
    bars = replay_and_dedup([early, late], sec_map)
    assert len(bars) == 1                       # deduped to one row
    assert bars[0].close == 9.99                # latest fetch won


def test_replay_dedup_unresolved_symbol_dropped():
    sec_map = {"AAPL": 1}                        # MSFT not in master
    m = reassemble({"pages": [{"bars": {
        "AAPL": [_raw("2020-01-02")], "MSFT": [_raw("2020-01-02")]}}]})
    bars = replay_and_dedup([m], sec_map)
    assert {b.security_id for b in bars} == {1}


def test_swap_and_rollback(tmp_path):
    cfg = AlpacaConfig(lake_root=tmp_path)
    live = cfg.silver_dir
    staging = _staging_dir(cfg)
    live.mkdir(parents=True)
    (live / "old.parquet").write_text("old")
    staging.mkdir(parents=True)
    (staging / "new.parquet").write_text("new")

    backup = swap_staging_into_live(cfg)

    assert (live / "new.parquet").exists()      # staging is now live
    assert not (live / "old.parquet").exists()
    assert (backup / "old.parquet").exists()    # old silver preserved in backup
    assert not staging.exists()