"""Metric helpers. Every reported metric carries a 95% interval."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence

from pydantic import BaseModel

TIERS = ("weak", "review", "strong")


class Estimate(BaseModel):
    value: float | None
    lower: float | None
    upper: float | None
    n: int

    @classmethod
    def empty(cls) -> Estimate:
        return cls(value=None, lower=None, upper=None, n=0)


def wilson(successes: int, n: int, z: float = 1.96) -> Estimate:
    if n == 0:
        return Estimate.empty()
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return Estimate(value=p, lower=max(0.0, centre - half), upper=min(1.0, centre + half), n=n)


def accuracy(pairs: Sequence[tuple[str, str]]) -> float:
    return sum(a == b for a, b in pairs) / len(pairs) if pairs else float("nan")


def cohen_kappa(pairs: Sequence[tuple[str, str]], labels: Sequence[str] = TIERS) -> float:
    n = len(pairs)
    if n == 0:
        return float("nan")
    po = sum(a == b for a, b in pairs) / n
    pe = sum((sum(a == lb for a, _ in pairs) / n) * (sum(b == lb for _, b in pairs) / n) for lb in labels)
    if pe >= 1:
        return 1.0 if po >= 1 else 0.0
    return (po - pe) / (1 - pe)


def bootstrap(pairs: Sequence, stat: Callable[[Sequence], float], *, n_resamples: int = 1000, seed: int = 0) -> Estimate:
    if not pairs:
        return Estimate.empty()
    rng = random.Random(seed)
    point = stat(pairs)
    values = []
    for _ in range(n_resamples):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        v = stat(sample)
        if not math.isnan(v):
            values.append(v)
    values.sort()
    if not values:
        return Estimate(value=point, lower=None, upper=None, n=len(pairs))
    lo = values[int(0.025 * (len(values) - 1))]
    hi = values[int(0.975 * (len(values) - 1))]
    return Estimate(value=point, lower=lo, upper=hi, n=len(pairs))


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")
