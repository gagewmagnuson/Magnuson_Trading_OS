"""
Derive normalized corporate actions from a Tiingo daily series.

Tiingo embeds actions inline: a row has divCash > 0 on a dividend's ex-date, and
splitFactor != 1 on a split's ex-date (splitFactor = splitTo/splitFrom). We
recover the TRUE split ratio (not a lossy 1->factor) via Fraction, so the
canonical event store retains the real financial event (4-for-1, 3-for-2,
1-for-8, ...), with a guard that rejects a rationalization that doesn't
reproduce the factor.
"""
from __future__ import annotations

from datetime import date
from fractions import Fraction

from trading_os.engine.adjust import Action

# splitFactor within this of 1.0 counts as "no split".
_SPLIT_EPS = 1e-9
# rationalized ratio must reproduce the raw factor within this relative tol.
_RATIO_TOL = 1e-6


def _row_date(row: dict) -> date:
    # Tiingo date is e.g. "2020-08-31T00:00:00.000Z"
    return date.fromisoformat(row["date"][:10])


def rationalize_split(split_factor: float) -> tuple[int, int, bool]:
    """
    Return (split_from, split_to, exact) for a Tiingo splitFactor
    (= split_to / split_from). exact=False means the small-integer ratio does
    NOT reproduce the factor within tolerance (caller should preserve the raw
    factor and flag it rather than store a wrong ratio).
    """
    frac = Fraction(split_factor).limit_denominator(1000)
    split_to, split_from = frac.numerator, frac.denominator
    if split_from == 0:
        return (1, 1, False)
    reproduced = split_to / split_from
    exact = abs(reproduced - split_factor) <= _RATIO_TOL * max(1.0, abs(split_factor))
    return (split_from, split_to, exact)


def derive_actions(security_id: int, rows: list[dict]) -> tuple[list[Action], list[str]]:
    """
    Build normalized Actions (SPLIT + CASH_DIVIDEND) from a Tiingo daily series.
    Returns (actions, warnings). Warnings flag any split factor that didn't
    rationalize cleanly (stored as split_from=1, split_to=factor as a fallback).
    """
    actions: list[Action] = []
    warnings: list[str] = []
    for row in rows:
        sf = float(row.get("splitFactor", 1.0) or 1.0)
        dc = float(row.get("divCash", 0.0) or 0.0)
        ex = _row_date(row)
        if abs(sf - 1.0) > _SPLIT_EPS:
            sfrom, sto, exact = rationalize_split(sf)
            if not exact:
                warnings.append(
                    f"{ex}: splitFactor {sf} did not rationalize to a small "
                    f"ratio; preserving raw factor as 1->{sf}"
                )
                sfrom, sto = 1, sf  # fallback: preserve the factor value
            actions.append(Action(security_id, "SPLIT", ex,
                                  split_from=sfrom, split_to=sto))
        if dc > 0.0:
            actions.append(Action(security_id, "CASH_DIVIDEND", ex, cash_amount=dc))
    return actions, warnings