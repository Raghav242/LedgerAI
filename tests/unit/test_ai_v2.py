"""Phase 6B AI regression tests: velocity features, model registry, schema validation,
per-family ensemble, hybrid decision, drift monitoring and the evaluation report."""

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from app.ai.drift import feature_drift, psi, status_of
from app.ai.ensemble import FAMILIES, GroupedIsolationForest
from app.ai.features import (
    FEATURE_NAMES,
    FEATURE_NAMES_V1,
    FeatureSchemaError,
    PriorStats,
    compute_features,
    prior_from_history,
    validate_features,
)
from app.ai.model import (
    ACTIVE_VERSION,
    VERSIONS,
    AnomalyModel,
    load_evaluation,
    load_metadata,
    metadata_path,
)
from app.ai.synthetic import SCENARIOS, featurize, generate

T = datetime(2026, 1, 1, tzinfo=UTC)


# --- features ------------------------------------------------------------------------------


def test_v2_schema_extends_v1_without_changing_it() -> None:
    assert FEATURE_NAMES[: len(FEATURE_NAMES_V1)] == FEATURE_NAMES_V1
    assert set(FEATURE_NAMES) - set(FEATURE_NAMES_V1) == {
        "gap_vs_typical",
        "burst_10s",
        "rate_vs_typical_60s",
    }


def test_cadence_comes_only_from_earlier_transfers() -> None:
    history = [(T + timedelta(seconds=s), 1_000) for s in (0, 10, 20, 30, 40)]
    at = T + timedelta(seconds=40.2)
    prior = prior_from_history(history, at)
    assert prior.median_gap_s == 10.0 and prior.count_last_10s == 1
    future = [*history, (at, 5), (at + timedelta(seconds=0.1), 5)]
    assert prior_from_history(future, at) == prior


def test_a_burst_is_far_below_the_accounts_usual_pace() -> None:
    history = [(T + timedelta(seconds=10 * i), 1_000) for i in range(10)]
    burst_at = T + timedelta(seconds=90.3)
    burst = compute_features(
        amount_minor=1_000,
        at=burst_at,
        account_created_at=T,
        prior=prior_from_history(history, burst_at),
    )
    usual_at = T + timedelta(seconds=100)
    usual = compute_features(
        amount_minor=1_000,
        at=usual_at,
        account_created_at=T,
        prior=prior_from_history(history, usual_at),
    )
    assert burst["gap_vs_typical"] < math.log(0.1)  # > 10x faster than usual
    assert abs(usual["gap_vs_typical"]) < 0.1  # the account's normal rhythm


def test_velocity_features_are_neutral_without_enough_history() -> None:
    few = prior_from_history([(T, 100), (T + timedelta(seconds=5), 100)], T + timedelta(seconds=6))
    assert few.median_gap_s is None  # one gap is not a cadence
    f = compute_features(
        amount_minor=100, at=T + timedelta(seconds=6), account_created_at=T, prior=few
    )
    assert f["gap_vs_typical"] == 0.0 and f["rate_vs_typical_60s"] == 0.0


def test_scenarios_leave_the_training_world_unchanged() -> None:
    assert generate(5, accounts=40) == generate(5, accounts=40, scenario=SCENARIOS["baseline"])
    shifted, _ = generate(5, accounts=40, scenario=SCENARIOS["fast_normal"])
    assert shifted != generate(5, accounts=40)[0]


# --- schema validation and registry ----------------------------------------------------------


def test_feature_schema_validation_rejects_missing_and_non_finite_values() -> None:
    ok = {name: 0.0 for name in FEATURE_NAMES}
    validate_features(ok, FEATURE_NAMES)
    with pytest.raises(FeatureSchemaError, match="missing"):
        validate_features({k: v for k, v in ok.items() if k != "burst_10s"}, FEATURE_NAMES)
    for bad in (math.nan, math.inf):
        with pytest.raises(FeatureSchemaError, match="non-finite"):
            validate_features({**ok, "gap_vs_typical": bad}, FEATURE_NAMES)


def test_every_registered_version_loads_verified_and_scores() -> None:
    assert ACTIVE_VERSION == VERSIONS[-1] == "iforest-v2"
    prior = PriorStats(count=20, median_amount_minor=3_000, last_at=T, count_last_60s=1)
    f = compute_features(
        amount_minor=3_100, at=T + timedelta(seconds=30), account_created_at=T, prior=prior
    )
    for version in VERSIONS:
        model = AnomalyModel.load_version(version)  # digest + scikit-learn version checked
        assert model.version == version
        assert 0 <= model.score(f) <= 1.5
    v2 = AnomalyModel.load_version("iforest-v2")
    with pytest.raises(FeatureSchemaError):
        v2.score({k: v for k, v in f.items() if k != "rate_vs_typical_60s"})
    with pytest.raises(ValueError, match="unknown model version"):
        AnomalyModel.load_version("iforest-v99")


def test_hybrid_decision_is_model_or_rules_and_keeps_both_verdicts() -> None:
    v2 = AnomalyModel.load_version("iforest-v2")
    assert v2.decision == "hybrid"
    assert v2.decide(v2.threshold, []) and v2.decide(0.0, ["rule"]) and not v2.decide(0.0, [])
    prior = PriorStats(
        count=20, median_amount_minor=3_000, last_at=T, count_last_60s=1, median_gap_s=10
    )
    huge = compute_features(
        amount_minor=3_000 * 40, at=T + timedelta(seconds=30), account_created_at=T, prior=prior
    )
    prediction = v2.predict(huge)
    assert prediction.is_anomaly and prediction.rule_flags and prediction.decision == "hybrid"
    assert prediction.model_flag == (prediction.score >= prediction.threshold)


def test_grouped_forest_is_deterministic_and_scores_are_percentiles() -> None:
    rows = featurize(*generate(11, accounts=60))
    x = np.asarray([[r.features[n] for n in FEATURE_NAMES] for r in rows])
    a = GroupedIsolationForest(feature_names=FEATURE_NAMES, seed=11, n_estimators=30).fit(x)
    b = GroupedIsolationForest(feature_names=FEATURE_NAMES, seed=11, n_estimators=30).fit(x)
    sa, sb = a.anomaly_score(x), b.anomaly_score(x)
    assert np.array_equal(sa, sb)
    assert sa.min() >= 0 and sa.max() <= 1
    assert set(a.family_scores(x)) == set(FAMILIES)


# --- evaluation report --------------------------------------------------------------------


def test_evaluation_report_covers_every_detector_dataset_and_metric() -> None:
    report = load_evaluation()
    assert report is not None
    datasets = report["datasets"]
    assert "held_out_accounts" in datasets
    assert sum(k.startswith("fresh_seed_") for k in datasets) >= 3
    assert {"shift_amount_shift", "shift_fast_normal", "shift_slow_bursts"} <= set(datasets)
    for ds in datasets.values():
        assert set(ds["detectors"]) == {"v1_model", "rules", "v2_model", "v2_hybrid"}
        for m in ds["detectors"].values():
            for key in ("precision", "recall", "f1", "false_positive_rate"):
                assert 0.0 <= m[key] <= 1.0
            assert set(m["recall_by_kind"]) == {"large", "burst", "new_account"}
    for version, card in report["models"].items():
        meta = json.loads(metadata_path(version).read_text(encoding="utf-8"))
        assert card["artifact_sha256"] == meta["artifact_sha256"]  # report matches artifacts
    assert set(report["inference_latency_ms_single_row_cpu"]) == set(VERSIONS)


def test_the_burst_blind_spot_is_fixed_on_unseen_accounts_and_worlds() -> None:
    report = load_evaluation()
    assert report is not None
    held_out = report["datasets"]["held_out_accounts"]["detectors"]
    assert held_out["v1_model"]["recall_by_kind"]["burst"] == 0.0  # the Phase 5E regression
    assert held_out["v2_model"]["recall_by_kind"]["burst"] >= 0.8
    fresh = report["fresh_seeds_summary"]
    assert fresh["v2_model"]["recall_by_kind"]["burst"]["mean"] >= 0.8
    slow = report["datasets"]["shift_slow_bursts"]["detectors"]
    # The fixed "< 1 s" rule breaks when bursts slow down; the cadence features do not.
    assert slow["v2_model"]["recall_by_kind"]["burst"] > slow["rules"]["recall_by_kind"]["burst"]


def test_retraining_reproduces_the_deployed_artifact(tmp_path: Path) -> None:
    """The documented command, in a fresh process, rebuilds the shipped file byte for byte.

    (In-process the pickle's bytes can differ with object sharing/interning; the scores
    cannot, which is checked too.)
    """
    import subprocess
    import sys

    import joblib

    subprocess.run(
        [sys.executable, "-m", "app.ai.train", "--out-dir", str(tmp_path)],
        check=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[2],
        timeout=600,
    )
    rebuilt = json.loads((tmp_path / "iforest-v2.json").read_text(encoding="utf-8"))
    shipped = load_metadata()
    assert shipped is not None
    assert rebuilt["artifact_sha256"] == shipped["artifact_sha256"]
    assert (rebuilt["threshold"], rebuilt["decision"]) == (
        shipped["threshold"],
        shipped["decision"],
    )
    x = np.random.default_rng(0).normal(size=(200, len(FEATURE_NAMES)))
    a = joblib.load(tmp_path / "iforest-v2.joblib")
    b = AnomalyModel.load_version("iforest-v2")._estimator
    assert np.array_equal(a.anomaly_score(x), b.anomaly_score(x))


# --- drift ---------------------------------------------------------------------------------


def test_psi_is_near_zero_for_the_training_distribution_and_large_for_a_shift() -> None:
    rng = np.random.default_rng(1)
    train = rng.normal(0, 1, 20_000)
    deciles = [float(np.quantile(train, q)) for q in np.linspace(0.1, 0.9, 9)]
    same = rng.normal(0, 1, 2_000).tolist()
    shifted = rng.normal(1.5, 1, 2_000).tolist()
    assert psi(same, deciles) < 0.05 and status_of(psi(same, deciles)) == "stable"
    assert status_of(psi(shifted, deciles)) == "significant"


def test_feature_drift_needs_a_minimum_sample_and_ranks_by_psi() -> None:
    meta = load_metadata()
    assert meta is not None
    reference = meta["reference"]
    assert all(len(reference[n]["deciles"]) == 9 for n in meta["features"])
    rows = [{**{n: reference[n]["median"] for n in meta["features"]}, "log_amount": 9.0}] * 60
    drift = feature_drift(rows, reference, meta["features"])
    assert drift[0].feature == "log_amount" and drift[0].status == "significant"
    assert feature_drift(rows[:10], reference, meta["features"]) == []
