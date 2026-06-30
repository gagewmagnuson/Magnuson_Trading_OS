"""
Corporate-action price/volume adjustment — the CAF (cumulative adjustment
factor) engine.

Pure and vendor-agnostic: it consumes normalized corporate-action rows and a
raw close lookup, and returns a multiplicative factor per (security_id,
session_date). It knows nothing about Alpaca, Postgres, or Parquet — the engine
layer feeds it data and applies the result on read. Raw bars are never mutated
(DEC-004); adjustment is purely a read-time transform.

THE MATH (back-adjustment, anchored to the query date `as_of`)
--------------------------------------------------------------
Each action contributes one multiplicative PRICE factor, computed at its own
ex-date and independent of every other action:

  * SPLIT (split_to-for-split_from; 4-for-1 => split_to=4, split_from=1):
        shares multiply by g = split_to / split_from
        price factor      f = split_from / split_to        (the inverse)
    A 4-for-1 split => f = 1/4; a 1-for-8 reverse split => f = 8.
    Splits need no price reference — pure ratio. Splits also move VOLUME (the
    share count changes), the only action type that does.

  * CASH_DIVIDEND of amount D, ex-date E, referencing the raw close C on the
    session BEFORE E:
        price factor f = (C - D) / C = 1 - D/C
    (CRSP/Yahoo convention: removes the mechanical ex-date drop so the series is
    continuous.) Dividends do NOT move volume.

For a bar on session date d, querying as-of T, the cumulative factor is the
product of every applicable action's factor whose ex-date falls AFTER that bar
and on/before T (and that was KNOWN by T):

    CAF(d; T) = Π f(a)  for every action a with  d < ex_date(a) <= T
                                            and  knowledge_time(a) <= T

    adjusted_price(d)  = raw_price(d)  * CAF(d; T)
    adjusted_volume(d) = raw_volume(d) / CAF_split(d; T)   # split sub-product only

Bars after the last applicable action get CAF = 1, so the most recent prices
keep their real values and history scales onto that basis. Because the factors
multiply, order is irrelevant and there is no compounding drift — five actions
or fifty, it is one product per bar.

PIT correctness: the SAME `as_of` that filters bars filters the action set
(ex_date <= T AND knowledge_time <= T), so "what did the adjusted chart look
like as known on date T" is exactly reconstructable, and an action announced
after T never bends earlier history.

adjustment modes:
  None          -> no factors (raw)
  "split"       -> splits only (continuous PRICE series)
  "total_return"-> splits + cash dividends (total-return series)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

SPLIT = "SPLIT"
CASH_DIVIDEND = "CASH_DIVIDEND"

VALID_MODES = (None, "split", "total_return")


@dataclass(frozen=True)
class Action:
    """A normalized corporate action (mirrors corp.corporate_action's adjustable
    subset). split_from/split_to set for SPLIT; cash_amount set for
    CASH_DIVIDEND."""
    security_id: int
    action_type: str
    ex_date: date
    split_from: float | None = None
    split_to: float | None = None
    cash_amount: float | None = None


@dataclass(frozen=True)
class Factor:
    """The cumulative factor for one (security_id, session_date)."""
    price_factor: float        # multiply raw price by this
    volume_factor: float       # multiply raw volume by this (= 1/split sub-product)


def _split_price_factor(a: Action) -> float:
    if not a.split_from or not a.split_to:
        raise ValueError(f"SPLIT missing ratio: {a}")
    # price factor = split_from / split_to (inverse of the share multiplier)
    return float(a.split_from) / float(a.split_to)


def compute_adjustment_factors(
    actions: list[Action],
    closes_by_security: dict[int, dict[date, float]],
    sessions_by_security: dict[int, list[date]],
    mode: str | None,
) -> dict[tuple[int, date], Factor]:
    """
    Build the cumulative factor per (security_id, session_date).

    actions:             PIT-filtered actions (ex_date <= as_of, known by as_of).
                         Caller is responsible for that filtering.
    closes_by_security:  {security_id: {session_date: raw_close}} — used to find
                         the day-before-ex close for dividend factors.
    sessions_by_security:{security_id: [sorted session_date, ...]} — the sessions
                         to emit factors for (typically the security's full bar
                         history, so edge dividends get a correct reference).
    mode:                None | "split" | "total_return".

    Returns {(security_id, session_date): Factor}. With mode=None every factor is
    identity (1.0, 1.0).
    """
    if mode not in VALID_MODES:
        raise ValueError(f"adjustment mode must be one of {VALID_MODES}, got {mode!r}")

    out: dict[tuple[int, date], Factor] = {}
    by_sec: dict[int, list[Action]] = {}
    for a in actions:
        by_sec.setdefault(a.security_id, []).append(a)

    for sec_id, sessions in sessions_by_security.items():
        sec_actions = by_sec.get(sec_id, []) if mode is not None else []
        # Pre-compute each action's (ex_date, price_factor, is_split).
        prepared: list[tuple[date, float, bool]] = []
        for a in sec_actions:
            if a.action_type == SPLIT:
                prepared.append((a.ex_date, _split_price_factor(a), True))
            elif a.action_type == CASH_DIVIDEND and mode == "total_return":
                ref_close = _day_before_ex_close(
                    a.ex_date, sessions, closes_by_security.get(sec_id, {})
                )
                if ref_close is None or ref_close <= 0:
                    # No reference close (e.g. dividend before bar history) ->
                    # cannot compute; skip (factor 1) rather than corrupt.
                    continue
                d = float(a.cash_amount or 0.0)
                prepared.append((a.ex_date, (ref_close - d) / ref_close, False))
            # CASH_DIVIDEND under mode="split" is ignored by design.

        for sd in sessions:
            price_f = 1.0
            split_f = 1.0
            for ex, f, is_split in prepared:
                if sd < ex:                       # action is AFTER this bar
                    price_f *= f
                    if is_split:
                        split_f *= f
            out[(sec_id, sd)] = Factor(
                price_factor=price_f,
                volume_factor=(1.0 / split_f) if split_f else 1.0,
            )
    return out


def _day_before_ex_close(
    ex_date: date, sessions: list[date], closes: dict[date, float]
) -> float | None:
    """The raw close on the latest session strictly before ex_date."""
    prev = None
    for sd in sessions:               # sessions is sorted ascending
        if sd < ex_date:
            prev = sd
        else:
            break
    if prev is None:
        return None
    return closes.get(prev)


# --------------------------- apply factors to bars (pure) ---------------------------
# Bar tuple layout (matches DuckDBStore.bars_eod_asof return shape):
#   0 security_id, 1 symbol, 2 session_date, 3 open, 4 high, 5 low, 6 close,
#   7 volume, 8 trade_count, 9 vwap, 10 knowledge_time, 11 source
def apply_factors_to_bars(bars, factors):
    """
    Apply per-(security_id, session_date) factors to raw bar tuples, returning
    new tuples (raw input is never mutated). Prices (open/high/low/close/vwap)
    multiply by price_factor; volume multiplies by volume_factor (rounded to int);
    trade_count, knowledge_time, source pass through unchanged. Bars with no
    factor or an identity factor are returned as-is.
    """
    out = []
    for b in bars:
        f = factors.get((b[0], b[2]))
        if f is None or (f.price_factor == 1.0 and f.volume_factor == 1.0):
            out.append(b)
            continue
        pf, vf = f.price_factor, f.volume_factor
        out.append((
            b[0], b[1], b[2],
            b[3] * pf, b[4] * pf, b[5] * pf, b[6] * pf,
            (int(round(b[7] * vf)) if b[7] is not None else b[7]),
            b[8],
            (b[9] * pf if b[9] is not None else b[9]),
            b[10], b[11],
        ))
    return out