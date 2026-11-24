"""Transaction features for anomaly scoring (Phase 5E).

ONE pure function, `compute_features`, used both to build the training data (replaying a
synthetic event stream) and by the AI worker at inference time (from SQL aggregates). Its
only inputs are the transfer itself and `PriorStats`: facts about the SOURCE account that
were known strictly BEFORE the transfer's own timestamp. Nothing about later transfers
can leak in, by construction; tests/unit/test_ai.py checks it by appending future events.

All features are on log scales so a few extreme values do not dominate.

Feature schema v2 (Phase 6B) adds three VELOCITY features relative to the account's own
cadence. v1 only had absolute ones: `burst_60s` (a fast but normal account also makes ~11
transfers a minute) and `log_secs_since_prev` (one extreme dimension out of six barely
shortens an Isolation Forest path). The v1 model therefore never flagged a burst (recall
0.0 on held-out accounts). The new features compare "now" with what THIS account usually
does, still using only transfers strictly before the one being scored:

  gap_vs_typical        log((gap to previous + 0.1) / (median earlier gap + 0.1));
                        strongly negative inside a burst, ~0 for the account's usual pace
  burst_10s             earlier transfers in the preceding 10 s
  rate_vs_typical_60s   log((transfers in the last 60 s + 1) / (expected in 60 s + 1)),
                        expected = 60 / median earlier gap
"""

import itertools
import math
from dataclasses import dataclass
from datetime import datetime

FEATURE_NAMES_V1 = (
    "log_amount",  # log1p(amount in dollars)
    "amount_vs_baseline",  # log of amount / median of this account's earlier transfers
    "log_prior_transfers",  # log1p(number of earlier transfers from this account)
    "burst_60s",  # earlier transfers from this account in the preceding 60 s
    "log_secs_since_prev",  # log(0.1 + seconds since this account's previous transfer)
    "log_account_age",  # log1p(seconds since the account was created)
)
FEATURE_NAMES = (
    *FEATURE_NAMES_V1,
    "gap_vs_typical",  # log((gap + 0.1) / (median earlier gap + 0.1)); 0 without history
    "burst_10s",  # earlier transfers from this account in the preceding 10 s
    "rate_vs_typical_60s",  # log((count in 60 s + 1) / (expected count in 60 s + 1))
)
FEATURE_SCHEMA_VERSION = 2
HISTORY_WINDOW = 200  # baseline medians use at most the last 200 earlier transfers
BURST_WINDOW_SECONDS = 60.0
SHORT_WINDOW_SECONDS = 10.0
SINCE_OFFSET = 0.1
MIN_GAPS_FOR_CADENCE = 2  # a typical gap needs at least two earlier gaps


class FeatureSchemaError(ValueError):
    """Features that do not match what a model was trained on (never scored)."""


def validate_features(features: dict[str, float], names: tuple[str, ...] | list[str]) -> None:
    """Every expected feature present and finite; raises FeatureSchemaError otherwise."""
    missing = [n for n in names if n not in features]
    if missing:
        raise FeatureSchemaError(f"missing features: {missing}")
    bad = [n for n in names if not math.isfinite(features[n])]
    if bad:
        raise FeatureSchemaError(f"non-finite features: {bad}")


@dataclass(frozen=True, slots=True)
class PriorStats:
    """What was known about the source account just before the transfer."""

    count: int  # earlier outgoing transfers (all time)
    median_amount_minor: float | None  # median of the last HISTORY_WINDOW of them
    last_at: datetime | None  # time of the most recent one
    count_last_60s: int  # how many of them in the preceding 60 s
    # Schema v2: median seconds between consecutive earlier transfers (last HISTORY_WINDOW;
    # None with fewer than MIN_GAPS_FOR_CADENCE gaps) and the count in the preceding 10 s.
    median_gap_s: float | None = None
    count_last_10s: int = 0


def compute_features(
    *, amount_minor: int, at: datetime, account_created_at: datetime, prior: PriorStats
) -> dict[str, float]:
    age = max(0.0, (at - account_created_at).total_seconds())
    since_prev = (at - prior.last_at).total_seconds() if prior.last_at is not None else age
    baseline = (
        math.log((amount_minor + 1) / (prior.median_amount_minor + 1))
        if prior.median_amount_minor is not None
        else 0.0  # no history: neither high nor low relative to itself
    )
    if prior.median_gap_s is not None and prior.last_at is not None:
        typical = prior.median_gap_s + SINCE_OFFSET
        gap_vs_typical = math.log((max(0.0, since_prev) + SINCE_OFFSET) / typical)
        expected_60s = BURST_WINDOW_SECONDS / typical
        rate_vs_typical = math.log((prior.count_last_60s + 1) / (expected_60s + 1))
    else:
        gap_vs_typical = rate_vs_typical = 0.0  # no cadence yet: neutral
    return {
        "log_amount": math.log1p(amount_minor / 100),
        "amount_vs_baseline": baseline,
        "log_prior_transfers": math.log1p(prior.count),
        "burst_60s": float(prior.count_last_60s),
        # log(0.1 + s), not log1p: keeps sub-second gaps (bursts) distinguishable.
        "log_secs_since_prev": math.log(SINCE_OFFSET + max(0.0, since_prev)),
        "log_account_age": math.log1p(age),
        "gap_vs_typical": gap_vs_typical,
        "burst_10s": float(prior.count_last_10s),
        "rate_vs_typical_60s": rate_vs_typical,
    }


def vector(
    features: dict[str, float], names: tuple[str, ...] | list[str] = FEATURE_NAMES
) -> list[float]:
    return [features[name] for name in names]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def prior_from_history(history: list[tuple[datetime, int]], at: datetime) -> PriorStats:
    """PriorStats from an in-memory list of (time, amount) of the account's transfers.

    Only entries strictly before `at` are used, whatever else the list contains.
    """
    earlier = sorted((t, a) for t, a in history if t < at)
    if not earlier:
        return PriorStats(count=0, median_amount_minor=None, last_at=None, count_last_60s=0)
    window = earlier[-HISTORY_WINDOW:]
    median = _median([float(a) for _, a in window])
    gaps = [(b[0] - a[0]).total_seconds() for a, b in itertools.pairwise(window)]
    recent = sum(1 for t, _ in earlier if (at - t).total_seconds() <= BURST_WINDOW_SECONDS)
    short = sum(1 for t, _ in earlier if (at - t).total_seconds() <= SHORT_WINDOW_SECONDS)
    return PriorStats(
        count=len(earlier),
        median_amount_minor=median,
        last_at=earlier[-1][0],
        count_last_60s=recent,
        median_gap_s=_median(gaps) if len(gaps) >= MIN_GAPS_FOR_CADENCE else None,
        count_last_10s=short,
    )


def describe(features: dict[str, float], reference: dict[str, dict[str, float]]) -> list[str]:
    """Plain-language notes on the features that deviate most from the training data.

    This is feature DEVIATION (how unusual each input is against the training population),
    not an attribution of the model's decision.
    """
    notes: list[tuple[float, str]] = []
    ratio = math.exp(features["amount_vs_baseline"])
    priors = round(math.expm1(features["log_prior_transfers"]))
    age = math.expm1(features["log_account_age"])
    since = math.exp(features["log_secs_since_prev"]) - SINCE_OFFSET
    for name in FEATURE_NAMES:
        ref = reference.get(name)
        if name not in features or not ref or (name == "log_secs_since_prev" and priors == 0):
            continue  # a first transfer has no "previous" one to be close to
        spread = max(ref["p95"] - ref["p05"], 1e-6)
        if features[name] > ref["p95"]:
            deviation = (features[name] - ref["p95"]) / spread
        elif features[name] < ref["p05"]:
            deviation = (ref["p05"] - features[name]) / spread
        else:
            continue
        text = {
            "log_amount": (
                f"Amount ${math.expm1(features['log_amount']):,.2f} is outside the usual range"
            ),
            "amount_vs_baseline": (
                f"Amount is {ratio:.1f}x this account's usual amount"
                if ratio >= 1
                else f"Amount is {1 / ratio:.1f}x smaller than this account's usual amount"
            ),
            "log_prior_transfers": f"{priors} earlier transfers from this account",
            "burst_60s": (
                f"{int(features['burst_60s'])} transfers from this account in the previous 60 s"
            ),
            "log_secs_since_prev": f"{since:.1f} s since this account's previous transfer",
            "log_account_age": f"Account is {age:.0f} s old",
            "gap_vs_typical": (
                f"Gap to the previous transfer is "
                f"{1 / math.exp(features['gap_vs_typical']):.0f}x shorter than usual"
                if features["gap_vs_typical"] < 0
                else f"Gap is {math.exp(features['gap_vs_typical']):.0f}x longer than usual"
            ),
            "burst_10s": f"{int(features['burst_10s'])} transfers in the previous 10 s",
            "rate_vs_typical_60s": (
                f"{math.exp(features['rate_vs_typical_60s']):.1f}x this account's usual "
                "rate over the last minute"
            ),
        }[name]
        notes.append((deviation, text))
    return [text for _, text in sorted(notes, reverse=True)[:3]]
