"""
Gold layer — materialized wide feature tables (DEC-028).

Repo path: src/trading_os/gold/__init__.py

Gold is the research-ready layer: one wide row per (security_id, session_date)
with the two canonical market observables (adj_close, adj_volume) and the Phase 1
derived features. Materialized to lake/gold/ Parquet, monthly-partitioned, read
directly by consumers (incl. the Research OS) via DuckDB — the Trading OS is a
batch PUBLISHER, not an online service (DEC-028 Decision 1).

PRICE SEMANTICS (documented ONCE, here, at the dataset level — NOT per feature):
  Gold is computed from the CANONICAL RESEARCH READ SURFACE, bars_eod_asof(...),
  at its configured research adjustment. TODAY that adjustment is 'split'
  (split-adjusted prices + volume, PIT-correct: only actions known by as_of are
  applied). Gold contains NO adjustment logic — it consumes whatever the read
  surface returns. If the canonical research adjustment ever changes (e.g. to
  total_return in a later version), Gold follows automatically; nothing here
  changes. adj_close/adj_volume are named to make this provenance explicit.

PIT PROPAGATION (DEC-028 Decision 3): each Gold row's knowledge_time = the
knowledge_time of the bar it was computed from (for EOD features the latest input
is the session's own bar). A consumer asking for Gold as-of D receives only rows
whose inputs were knowable by D.
"""
from __future__ import annotations

from datetime import date

import polars as pl

from trading_os.features import (
    simple_return, log_return, sma, ema, realized_vol, roc, momentum_12_1,
)

# The research adjustment Gold consumes from the canonical read surface.
# Change here (and nowhere else) if the canonical research adjustment changes.
RESEARCH_ADJUSTMENT = "split"

# Gold wide schema — column order is intentional (identity, time, observables,
# then features grouped: returns, moving averages, vol, momentum).
GOLD_COLUMNS = [
    "security_id", "symbol", "session_date", "knowledge_time",
    "adj_close", "adj_volume",
    "return_1d", "log_return_1d",
    "sma20", "sma50", "ema20",
    "realized_vol20",
    "roc20", "momentum_12_1",
]


def compute_gold_for_security(bars: pl.DataFrame) -> pl.DataFrame:
    """Compute the Phase 1 wide Gold rows for ONE security from its adjusted bar
    series. `bars` must be the canonical feature contract plus knowledge_time and
    adj_volume: columns (security_id, symbol, session_date, close, volume,
    knowledge_time), SORTED ascending by session_date, single security, where
    `close`/`volume` are the research-adjusted values from bars_eod_asof.

    Returns a wide frame with GOLD_COLUMNS. Feature functions are pure and operate
    on the canonical (security_id, session_date, close) contract; we assemble
    their outputs into the wide row here.
    """
    if bars.is_empty():
        return pl.DataFrame(schema={c: pl.Float64 for c in GOLD_COLUMNS})

    # Base frame the pure feature functions expect.
    base = bars.select("security_id", "session_date", "close")

    # Each feature returns (security_id, session_date, <col>); join on the keys.
    out = bars.select(
        "security_id", "symbol", "session_date", "knowledge_time",
        pl.col("close").alias("adj_close"),
        pl.col("volume").alias("adj_volume"),
    )
    feature_frames = [
        simple_return(base, period=1),
        log_return(base, period=1),
        sma(base, window=20),
        sma(base, window=50),
        ema(base, span=20),
        realized_vol(base, window=20),
        roc(base, window=20),
        momentum_12_1(base),
    ]
    for f in feature_frames:
        out = out.join(f, on=["security_id", "session_date"], how="left")

    return out.select(GOLD_COLUMNS)