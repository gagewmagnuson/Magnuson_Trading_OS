"""
Phase 1 Gold features — the blueprint V1 starter analytics set (DEC-028).

Repo path: src/trading_os/features/__init__.py

A feature PUBLISHER, not an engine (DEC-028 Decision 6). This module implements
EXACTLY the eight published Phase 1 features and nothing else. Adding a feature is
a deliberate act (a new function), never a config tweak — there is no generic
indicator API.

Purity contract (DEC-028 Model (a)):
  - Functions are pure math on a canonical, already-PIT-correct bar series. They
    know nothing about knowledge_time, security identity semantics, or universes.
    PIT-correctness is guaranteed UPSTREAM (the caller fetches via bars_eod_asof)
    and stamped DOWNSTREAM (the Gold writer sets knowledge_time = max input kt).
  - Input: a Polars DataFrame with columns (security_id, session_date, close),
    SORTED ascending by session_date, single security. Callers guarantee the sort.
  - Output: a new DataFrame (security_id, session_date, <feature_column>). Never
    mutates the input.
  - Rolling features return NULL during warm-up (window not yet full). No
    forward-fill, no back-fill, no dropping early rows — a feature that isn't yet
    computable is honestly null.
"""
from __future__ import annotations

import polars as pl

# The canonical input columns every feature expects.
CANON = ("security_id", "session_date", "close")

_TRADING_DAYS_YEAR = 252   # annualization / momentum lookback basis


def _check(df: pl.DataFrame) -> None:
    missing = [c for c in CANON if c not in df.columns]
    if missing:
        raise ValueError(f"feature input missing canonical columns: {missing}")


def _out(df: pl.DataFrame, col: str, expr: pl.Expr) -> pl.DataFrame:
    """Return (security_id, session_date, <col>) without mutating df."""
    return df.select("security_id", "session_date", expr.alias(col))


# ---- returns -------------------------------------------------------------

def simple_return(df: pl.DataFrame, period: int = 1) -> pl.DataFrame:
    """Simple return over `period` sessions: close[T]/close[T-period] - 1.
    Null for the first `period` rows."""
    _check(df)
    col = f"return_{period}d"
    expr = (pl.col("close") / pl.col("close").shift(period) - 1.0)
    return _out(df, col, expr)


def log_return(df: pl.DataFrame, period: int = 1) -> pl.DataFrame:
    """Log return over `period` sessions: ln(close[T]/close[T-period]).
    Null for the first `period` rows."""
    _check(df)
    col = f"log_return_{period}d"
    expr = (pl.col("close") / pl.col("close").shift(period)).log()
    return _out(df, col, expr)


# ---- moving averages -----------------------------------------------------

def sma(df: pl.DataFrame, window: int) -> pl.DataFrame:
    """Simple moving average of close over `window` sessions. Null until the
    window is full (first window-1 rows)."""
    _check(df)
    col = f"sma{window}"
    expr = pl.col("close").rolling_mean(window_size=window)
    return _out(df, col, expr)


def ema(df: pl.DataFrame, span: int) -> pl.DataFrame:
    """Exponential moving average of close (span form; alpha = 2/(span+1)).
    Uses Polars ewm_mean. First value seeded at close[0] (standard EWMA warm-up);
    not null, matching pandas/polars ewm convention with adjust=False."""
    _check(df)
    col = f"ema{span}"
    expr = pl.col("close").ewm_mean(span=span, adjust=False)
    return _out(df, col, expr)


# ---- volatility ----------------------------------------------------------

def realized_vol(df: pl.DataFrame, window: int) -> pl.DataFrame:
    """Annualized realized volatility: std of daily log returns over `window`
    sessions, x sqrt(252). Null until window+1 closes exist (need `window` log
    returns, which need window+1 prices). Sample std (ddof=1)."""
    _check(df)
    col = f"realized_vol{window}"
    logret = (pl.col("close") / pl.col("close").shift(1)).log()
    expr = logret.rolling_std(window_size=window, ddof=1) * (float(_TRADING_DAYS_YEAR) ** 0.5)
    return _out(df, col, expr)


# ---- momentum ------------------------------------------------------------

def roc(df: pl.DataFrame, window: int) -> pl.DataFrame:
    """Rate of change over `window` sessions: close[T]/close[T-window] - 1.
    (Same math as simple_return but named/registered as a momentum indicator over
    a longer window.) Null for the first `window` rows."""
    _check(df)
    col = f"roc{window}"
    expr = (pl.col("close") / pl.col("close").shift(window) - 1.0)
    return _out(df, col, expr)


def momentum_12_1(df: pl.DataFrame) -> pl.DataFrame:
    """Academic 12-1 momentum: the ~12-month return EXCLUDING the most recent
    ~1 month. Concretely close[T-21] / close[T-252] - 1 — the return from ~12
    months ago to ~1 month ago, skipping the last 21 sessions to avoid short-term
    reversal. Null until 252 prior sessions exist."""
    _check(df)
    col = "momentum_12_1"
    # numerator = price 21 sessions ago; denominator = price 252 sessions ago.
    expr = (pl.col("close").shift(21) / pl.col("close").shift(_TRADING_DAYS_YEAR) - 1.0)
    return _out(df, col, expr)