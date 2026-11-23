"""Transaction features for anomaly scoring (Phase 5E).

ONE pure function, `compute_features`, used both to build the training data (replaying a
synthetic event stream) and by the AI worker at inference time (from SQL aggregates). Its
only inputs are the transfer itself and `PriorStats`: facts about the SOURCE account that
were known strictly BEFORE the transfer's own timestamp. Nothing about later transfers
can leak in, by construction; tests/unit/test_ai.py checks it by appending future events.

All features are on log scales so a few extreme values do not dominate.
"""

import math
from dataclasses import dataclass
from datetime import datetime

FEATURE_NAMES = (
    "log_amount",  # log1p(amount in dollars)
    "amount_vs_baseline",  # log of amount / median of this account's earlier transfers
    "log_prior_transfers",  # log1p(number of earlier transfers from this account)
    "burst_60s",  # earlier transfers from this account in the preceding 60 s
    "log_secs_since_prev",  # log(0.1 + seconds since this account's previous transfer)
    "log_account_age",  # log1p(seconds since the account was created)
)
HISTORY_WINDOW = 200  # the baseline median uses at most the last 200 earlier transfers
BURST_WINDOW_SECONDS = 60.0
SINCE_OFFSET = 0.1


@dataclass(frozen=True, slots=True)
class PriorStats:
    """What was known about the source account just before the transfer."""

    count: int  # earlier outgoing transfers (all time)
    median_amount_minor: float | None  # median of the last HISTORY_WINDOW of them
    last_at: datetime | None  # time of the most recent one
    count_last_60s: int  # how many of them in the preceding 60 s


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
    return {
        "log_amount": math.log1p(amount_minor / 100),
        "amount_vs_baseline": baseline,
        "log_prior_transfers": math.log1p(prior.count),
        "burst_60s": float(prior.count_last_60s),
        # log(0.1 + s), not log1p: keeps sub-second gaps (bursts) distinguishable.
        "log_secs_since_prev": math.log(SINCE_OFFSET + max(0.0, since_prev)),
        "log_account_age": math.log1p(age),
    }


def vector(features: dict[str, float]) -> list[float]:
    return [features[name] for name in FEATURE_NAMES]


def prior_from_history(history: list[tuple[datetime, int]], at: datetime) -> PriorStats:
    """PriorStats from an in-memory list of (time, amount) of the account's transfers.

    Only entries strictly before `at` are used, whatever else the list contains.
    """
    earlier = sorted((t, a) for t, a in history if t < at)
    if not earlier:
        return PriorStats(count=0, median_amount_minor=None, last_at=None, count_last_60s=0)
    window = sorted(a for _, a in earlier[-HISTORY_WINDOW:])
    mid = len(window) // 2
    median = window[mid] if len(window) % 2 else (window[mid - 1] + window[mid]) / 2
    recent = sum(1 for t, _ in earlier if (at - t).total_seconds() <= BURST_WINDOW_SECONDS)
    return PriorStats(
        count=len(earlier),
        median_amount_minor=float(median),
        last_at=earlier[-1][0],
        count_last_60s=recent,
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
        if not ref or (name == "log_secs_since_prev" and priors == 0):
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
        }[name]
        notes.append((deviation, text))
    return [text for _, text in sorted(notes, reverse=True)[:3]]
