"""AI pipeline units: reproducible data, no future information, held-out evaluation,
model loading and CPU inference, rule baseline and explanation wording."""

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.ai.features import (
    FEATURE_NAMES,
    PriorStats,
    compute_features,
    describe,
    prior_from_history,
)
from app.ai.model import ARTIFACT, AnomalyModel, load_metadata, rule_flags
from app.ai.synthetic import featurize, generate, split_by_account

T = datetime(2026, 1, 1, tzinfo=UTC)


def test_synthetic_data_is_reproducible() -> None:
    a, created_a = generate(7, accounts=60)
    b, created_b = generate(7, accounts=60)
    c, _ = generate(8, accounts=60)
    assert a == b and created_a == created_b
    assert a != c
    assert {x.label for x in a} >= {"normal", "new_account"}


def test_features_ignore_everything_at_or_after_the_event_time() -> None:
    history = [(T + timedelta(seconds=s), 1_000 + s) for s in (0, 10, 20, 30)]
    at = T + timedelta(seconds=25)
    future = [*history, (at, 999_999), (at + timedelta(seconds=1), 999_999)]
    assert prior_from_history(history, at) == prior_from_history(future, at)
    prior = prior_from_history(history, at)
    assert (prior.count, prior.last_at, prior.count_last_60s) == (3, T + timedelta(seconds=20), 3)
    assert prior.median_amount_minor == 1_010  # median of 1000, 1010, 1020


def test_replayed_dataset_has_no_future_leakage() -> None:
    """Appending later activity to the stream never changes an earlier row's features."""
    transfers, created = generate(3, accounts=40)
    cut = len(transfers) // 2
    early = featurize(transfers[:cut], created)
    full = featurize(transfers, created)
    assert [r.features for r in early] == [r.features for r in full[:cut]]


def test_split_is_by_account_and_disjoint() -> None:
    rows = featurize(*generate(5, accounts=80))
    train, val, test = split_by_account(rows, 5)
    accounts = [{r.account for r in part} for part in (train, val, test)]
    assert not (accounts[0] & accounts[1] or accounts[0] & accounts[2] or accounts[1] & accounts[2])
    assert len(train) + len(val) + len(test) == len(rows)


def test_compute_features_without_history_is_neutral() -> None:
    empty = PriorStats(count=0, median_amount_minor=None, last_at=None, count_last_60s=0)
    f = compute_features(
        amount_minor=5_000, at=T + timedelta(seconds=10), account_created_at=T, prior=empty
    )
    assert tuple(f) == FEATURE_NAMES
    assert f["amount_vs_baseline"] == 0.0 and f["log_prior_transfers"] == 0.0
    assert math.isclose(f["log_account_age"], math.log1p(10))


def test_shipped_model_loads_and_scores_on_cpu() -> None:
    assert ARTIFACT.exists()
    model = AnomalyModel.load()
    meta = model.metadata
    assert meta["version"] == "iforest-v1" and tuple(meta["features"]) == FEATURE_NAMES
    usual = PriorStats(count=20, median_amount_minor=3_000, last_at=T, count_last_60s=1)
    normal = compute_features(
        amount_minor=3_100,
        at=T + timedelta(seconds=30),
        account_created_at=T - timedelta(seconds=600),
        prior=usual,
    )
    huge = {**normal, "amount_vs_baseline": math.log(40), "log_amount": math.log1p(1_200)}
    low, high = model.predict(normal), model.predict(huge)
    assert high.score > low.score
    assert high.is_anomaly and not low.is_anomaly
    assert low.inference_ms < 1_000


def test_evaluation_is_on_held_out_data_and_reported_honestly() -> None:
    meta = load_metadata()
    assert meta is not None
    ev = meta["evaluation"]
    assert "held-out" in ev["split"]
    for key in ("precision", "recall", "f1", "false_positive_rate"):
        assert 0.0 <= ev["model"][key] <= 1.0
        assert 0.0 <= ev["rule_baseline"][key] <= 1.0
    assert ev["inference_latency_ms_single_row_cpu"]["p50"] > 0
    assert sum(meta["dataset"]["test"].values()) > 0
    assert any("synthetic" in line for line in meta["limitations"])
    assert "Not a probability" in meta["score_meaning"]


def test_training_is_deterministic_for_a_seed() -> None:
    from sklearn.ensemble import IsolationForest

    rows = featurize(*generate(11, accounts=50))
    x = np.asarray([[r.features[n] for n in FEATURE_NAMES] for r in rows])
    a = IsolationForest(n_estimators=50, random_state=11).fit(x).score_samples(x[:20])
    b = IsolationForest(n_estimators=50, random_state=11).fit(x).score_samples(x[:20])
    assert np.array_equal(a, b)


@pytest.mark.parametrize(
    ("change", "flag"),
    [
        ({"amount_vs_baseline": math.log(10)}, "amount >= 8x"),
        (
            {"log_secs_since_prev": math.log(0.3), "log_prior_transfers": math.log1p(4)},
            "less than 1 s",
        ),
        ({"log_prior_transfers": 0.0, "log_amount": math.log1p(900)}, "first transfer"),
    ],
)
def test_rule_baseline(change: dict, flag: str) -> None:
    base = compute_features(
        amount_minor=3_000,
        at=T + timedelta(seconds=100),
        account_created_at=T,
        prior=PriorStats(
            count=5, median_amount_minor=3_000, last_at=T + timedelta(seconds=90), count_last_60s=1
        ),
    )
    assert rule_flags(base) == []
    assert any(flag in f for f in rule_flags({**base, **change}))


def test_explanations_describe_deviation_and_skip_previous_for_a_first_transfer() -> None:
    meta = load_metadata()
    assert meta is not None
    first = compute_features(
        amount_minor=60_000,
        at=T + timedelta(seconds=0.3),
        account_created_at=T,
        prior=PriorStats(count=0, median_amount_minor=None, last_at=None, count_last_60s=0),
    )
    notes = describe(first, meta["reference"])
    assert notes and not any("previous transfer" in n for n in notes)
    assert not any("probab" in n.lower() for n in notes)


def test_prior_stats_is_immutable() -> None:
    p = PriorStats(count=1, median_amount_minor=1.0, last_at=T, count_last_60s=0)
    assert replace(p, count=2).count == 2 and p.count == 1
