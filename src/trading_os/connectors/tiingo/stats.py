"""
Relative-error statistics + correlation/R2 for validating an adjusted-price
series against a reference. Pure stdlib math (no numpy dependency).

The distribution distinguishes error TYPES, not just magnitude:
  * missing dividend  -> mean/max rise, correlation stays ~1
  * date misalignment -> correlation drops even if per-point errors look modest
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorStats:
    n: int
    mean: float
    median: float
    p95: float
    max: float
    worst_date: str | None
    correlation: float
    r2: float

    def passes(self, tol: float) -> bool:
        return self.n > 0 and self.max < tol


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 1.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return 1.0 if sxx == syy else 0.0
    return sxy / math.sqrt(sxx * syy)


def compute_error_stats(
    pairs: list[tuple[str, float, float]]
) -> ErrorStats:
    """
    pairs: (date_str, ours, reference). Relative error = |ours-ref|/|ref|
    (ref==0 rows are skipped).
    """
    rels: list[tuple[float, str]] = []
    ours_v: list[float] = []
    ref_v: list[float] = []
    for d, ours, ref in pairs:
        if ref == 0:
            continue
        rels.append((abs(ours - ref) / abs(ref), d))
        ours_v.append(ours)
        ref_v.append(ref)
    if not rels:
        return ErrorStats(0, 0, 0, 0, 0, None, 1.0, 1.0)
    vals = sorted(r for r, _ in rels)
    worst = max(rels, key=lambda t: t[0])
    corr = _pearson(ours_v, ref_v)
    return ErrorStats(
        n=len(rels),
        mean=sum(vals) / len(vals),
        median=_percentile(vals, 0.5),
        p95=_percentile(vals, 0.95),
        max=worst[0],
        worst_date=worst[1],
        correlation=corr,
        r2=corr * corr,
    )