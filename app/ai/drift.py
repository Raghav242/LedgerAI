"""Lightweight input-drift monitoring (Phase 6B). Pure Python: the API image has no numpy.

Population Stability Index per feature: the training deciles (stored in the model metadata
by app/ai/train.py) split the line into 10 bins that each held 10 % of training rows; PSI
compares the share of RECENT live rows in each bin with that 10 %:

    PSI = sum over bins of (live - expected) * ln(live / expected)

Conventional reading: < 0.10 stable, 0.10-0.25 moderate shift, > 0.25 significant shift.
Drift is a signal to look, not proof the model is wrong: lab experiments deliberately
produce traffic unlike the synthetic training world, so drift is expected there.
"""

import bisect
import math
from dataclasses import dataclass
from typing import Any

MIN_SAMPLE = 50
EPSILON = 1e-4  # avoids log(0) for empty bins
STABLE, MODERATE = 0.10, 0.25


@dataclass(frozen=True, slots=True)
class FeatureDrift:
    feature: str
    psi: float
    status: str  # stable | moderate | significant
    live_median: float
    train_median: float


def psi(values: list[float], deciles: list[float]) -> float:
    counts = [0] * (len(deciles) + 1)
    for v in values:
        counts[bisect.bisect_right(deciles, v)] += 1
    expected = 1.0 / len(counts)
    total = 0.0
    for c in counts:
        live = max(c / len(values), EPSILON)
        total += (live - expected) * math.log(live / expected)
    return total


def status_of(value: float) -> str:
    if value < STABLE:
        return "stable"
    return "moderate" if value < MODERATE else "significant"


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def feature_drift(
    rows: list[dict[str, float]], reference: dict[str, dict[str, Any]], features: list[str]
) -> list[FeatureDrift]:
    out = []
    for name in features:
        ref = reference.get(name) or {}
        deciles = ref.get("deciles")
        values = [r[name] for r in rows if name in r]
        if not deciles or len(values) < MIN_SAMPLE:
            continue
        value = psi(values, deciles)
        out.append(
            FeatureDrift(
                feature=name,
                psi=round(value, 4),
                status=status_of(value),
                live_median=round(_median(values), 4),
                train_median=ref.get("median", 0.0),
            )
        )
    return sorted(out, key=lambda d: d.psi, reverse=True)
