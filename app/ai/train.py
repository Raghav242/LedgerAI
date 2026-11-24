"""Train and evaluate an anomaly model version on deterministic synthetic data.

    python -m app.ai.train                         # the active version (iforest-v2)
    python -m app.ai.train --version iforest-v1    # reproduce the Phase 5E model
    python -m app.ai.train --out-dir <dir>         # write elsewhere (reproducibility check)

Pipeline (all seeded, so the same command reproduces the same artifact byte for byte):
  1. generate synthetic activity and replay it into features (no future information)
  2. split 60/20/20 BY ACCOUNT into train / validation / test
  3. fit an IsolationForest on the TRAINING rows only (unsupervised: labels are not used)
  4. on the VALIDATION rows: pick the threshold that maximises F1 for the model alone and,
     for versions that allow it, for the hybrid (model OR rule baseline); adopt the hybrid
     only if its validation F1 is clearly better (the test split is never used to choose)
  5. report precision, recall, F1, false-positive rate and per-scenario recall on the
     held-out TEST accounts for the model, the hybrid and the rule baseline, plus CPU
     inference latency. Wider evaluation (more seeds, shifted worlds, v1 vs v2):
     app/ai/evaluate.py.
"""

import argparse
import json
import platform
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest

from app.ai.ensemble import FAMILIES, GroupedIsolationForest
from app.ai.features import FEATURE_NAMES, FEATURE_NAMES_V1, FEATURE_SCHEMA_VERSION, vector
from app.ai.model import ACTIVE_VERSION, MODEL_DIR, artifact_digest, rule_flags
from app.ai.synthetic import DEFAULT_SEED, Row, featurize, generate, split_by_account

N_ESTIMATORS = 200
MAX_SAMPLES = 256
KINDS = ("large", "burst", "new_account")
HYBRID_MIN_GAIN = 0.01  # validation F1 the hybrid must add to be adopted


@dataclass(frozen=True)
class TrainingConfig:
    version: str
    features: tuple[str, ...]
    allow_hybrid: bool
    feature_schema_version: int
    grouped: bool  # one forest per feature family (app/ai/ensemble.py) vs a single forest


CONFIGS = {
    "iforest-v1": TrainingConfig("iforest-v1", FEATURE_NAMES_V1, False, 1, grouped=False),
    "iforest-v2": TrainingConfig(
        "iforest-v2", FEATURE_NAMES, True, FEATURE_SCHEMA_VERSION, grouped=True
    ),
}


def unusualness(model: Any, x: np.ndarray) -> np.ndarray:
    """Higher = more unusual, for either model type."""
    if isinstance(model, GroupedIsolationForest):
        return model.anomaly_score(x)
    return -model.score_samples(x)


def fit(config: TrainingConfig, x: np.ndarray, seed: int) -> Any:
    if config.grouped:
        return GroupedIsolationForest(
            feature_names=config.features,
            seed=seed,
            n_estimators=N_ESTIMATORS,
            max_samples=MAX_SAMPLES,
        ).fit(x)
    return IsolationForest(
        n_estimators=N_ESTIMATORS, max_samples=MAX_SAMPLES, contamination="auto", random_state=seed
    ).fit(x)


def confusion(predicted: list[bool], actual: list[bool]) -> dict[str, float]:
    tp = sum(p and a for p, a in zip(predicted, actual, strict=True))
    fp = sum(p and not a for p, a in zip(predicted, actual, strict=True))
    fn = sum(a and not p for p, a in zip(predicted, actual, strict=True))
    tn = sum(not p and not a for p, a in zip(predicted, actual, strict=True))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "false_positive_rate": round(fp / (fp + tn) if fp + tn else 0.0, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def recall_by_kind(predicted: list[bool], rows: list[Row]) -> dict[str, float]:
    out = {}
    for kind in KINDS:
        hits = [p for p, r in zip(predicted, rows, strict=True) if r.label == kind]
        out[kind] = round(sum(hits) / len(hits), 4) if hits else 0.0
    return out


def metrics(predicted: list[bool], rows: list[Row]) -> dict[str, Any]:
    return {
        **confusion(predicted, [r.anomalous for r in rows]),
        "recall_by_kind": recall_by_kind(predicted, rows),
    }


def matrix(rows: list[Row], names: tuple[str, ...] = FEATURE_NAMES) -> np.ndarray:
    return np.asarray([vector(r.features, names) for r in rows])


def best_threshold(
    scores: np.ndarray, actual: list[bool], extra: list[bool] | None = None
) -> float:
    """Threshold maximising F1 of (score >= t) [OR extra] over a quantile grid."""
    best = (-1.0, float(np.quantile(scores, 0.97)))
    for q in np.linspace(0.80, 0.999, 400):
        t = float(np.quantile(scores, q))
        predicted = [bool(s >= t) for s in scores]
        if extra is not None:
            predicted = [p or e for p, e in zip(predicted, extra, strict=True)]
        f1 = confusion(predicted, actual)["f1"]
        if f1 > best[0]:
            best = (f1, t)
    return best[1]


def reference_stats(rows: list[Row], names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Training distribution per feature: p05/median/p95 for explanations, decile edges
    for drift monitoring (Population Stability Index against live inputs)."""
    x = matrix(rows, names)
    out: dict[str, dict[str, Any]] = {}
    for i, name in enumerate(names):
        col = x[:, i]
        out[name] = {
            "p05": round(float(np.quantile(col, 0.05)), 4),
            "median": round(float(np.median(col)), 4),
            "p95": round(float(np.quantile(col, 0.95)), 4),
            "deciles": [round(float(np.quantile(col, q)), 6) for q in np.linspace(0.1, 0.9, 9)],
        }
    return out


def inference_latency_ms(
    model: Any, rows: list[Row], names: tuple[str, ...], n: int = 300
) -> dict[str, float]:
    samples = []
    for row in rows[:n]:
        x = np.asarray([vector(row.features, names)])
        started = time.perf_counter()
        unusualness(model, x)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return {
        "p50": round(statistics.median(samples), 3),
        "p95": round(samples[int(len(samples) * 0.95) - 1], 3),
        "samples": len(samples),
    }


def counts(rows: list[Row]) -> dict[str, int]:
    return {k: sum(1 for r in rows if r.label == k) for k in ("normal", *KINDS)}


def train(
    config: TrainingConfig = CONFIGS[ACTIVE_VERSION], seed: int = DEFAULT_SEED
) -> tuple[Any, dict[str, Any]]:
    names = config.features
    rows = featurize(*generate(seed))
    train_rows, val_rows, test_rows = split_by_account(rows, seed)
    model = fit(config, matrix(train_rows, names), seed)  # labels are never shown to it

    val_actual = [r.anomalous for r in val_rows]
    val_scores = unusualness(model, matrix(val_rows, names))
    val_rules = [bool(rule_flags(r.features)) for r in val_rows]
    model_threshold = best_threshold(val_scores, val_actual)
    model_val_f1 = confusion([bool(s >= model_threshold) for s in val_scores], val_actual)["f1"]
    decision, threshold, hybrid_val_f1 = "model", model_threshold, None
    if config.allow_hybrid:
        hybrid_threshold = best_threshold(val_scores, val_actual, extra=val_rules)
        hybrid_val_f1 = confusion(
            [bool(s >= hybrid_threshold) or r for s, r in zip(val_scores, val_rules, strict=True)],
            val_actual,
        )["f1"]
        if hybrid_val_f1 >= model_val_f1 + HYBRID_MIN_GAIN:
            decision, threshold = "hybrid", hybrid_threshold

    test_scores = unusualness(model, matrix(test_rows, names))
    # The model alone at ITS OWN validation threshold; the hybrid at the hybrid threshold.
    model_pred = [bool(s >= model_threshold) for s in test_scores]
    rules = [bool(rule_flags(r.features)) for r in test_rows]
    at_deployed = [bool(s >= threshold) for s in test_scores]
    hybrid_pred = [m or r for m, r in zip(at_deployed, rules, strict=True)]
    deployed = hybrid_pred if decision == "hybrid" else model_pred

    metadata = {
        "version": config.version,
        "algorithm": (
            f"one scikit-learn IsolationForest per feature family {sorted(FAMILIES)} "
            f"(n_estimators={N_ESTIMATORS}, max_samples={MAX_SAMPLES}); score = max over "
            "families of the training percentile"
            if config.grouped
            else f"scikit-learn IsolationForest (n_estimators={N_ESTIMATORS}, "
            f"max_samples={MAX_SAMPLES})"
        ),
        "families": {k: list(v) for k, v in FAMILIES.items()} if config.grouped else None,
        "model_threshold": round(model_threshold, 6),
        "features": list(names),
        "feature_schema_version": config.feature_schema_version,
        "decision": decision,
        "decision_rule": (
            "hybrid = model score >= threshold OR any rule-baseline flag; adopted because "
            f"its validation F1 ({hybrid_val_f1}) beat the model alone ({model_val_f1}) by "
            f">= {HYBRID_MIN_GAIN}"
            if decision == "hybrid"
            else f"model score >= threshold (validation F1 {model_val_f1})"
        ),
        "threshold": round(threshold, 6),
        "threshold_rule": f"maximises {decision} F1 on the validation split",
        "validation": {"model_f1": model_val_f1, "hybrid_f1": hybrid_val_f1},
        "score_meaning": (
            "0-1: the highest training percentile of this transfer's unusualness in any "
            "feature family (0.99 = more unusual than 99% of training transfers in at least "
            "one respect). Not a probability."
            if config.grouped
            else "negated IsolationForest score_samples; higher = more unusual. Not a probability."
        ),
        "seed": seed,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": {
            "python": platform.python_version(),
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
        },
        "dataset": {
            "kind": "synthetic, compressed clock (see app/ai/synthetic.py)",
            "rows": len(rows),
            "split": "60/20/20 by account",
            "train": counts(train_rows),
            "validation": counts(val_rows),
            "test": counts(test_rows),
        },
        "reference": reference_stats(train_rows, names),
        "expected_anomaly_rate": round(
            float(np.mean([bool(s >= threshold) for s in val_scores])), 4
        ),
        "evaluation": {
            "split": "held-out test accounts (never seen in training or threshold selection)",
            "deployed": metrics(deployed, test_rows),
            "model": metrics(model_pred, test_rows),
            "hybrid": metrics(hybrid_pred, test_rows),
            "rule_baseline": metrics(rules, test_rows),
            "inference_latency_ms_single_row_cpu": inference_latency_ms(model, test_rows, names),
        },
        "limitations": [
            "Trained and evaluated on synthetic data only; says nothing about real-world fraud.",
            "Labels are the generator's intent; the model never sees them during training.",
            "Compressed clock: seconds stand in for hours or days so lab scenarios run live.",
            "Only the source account's own history is used (no counterparties, devices, "
            "geography).",
            "The score is relative unusualness, not a probability.",
            "Velocity features need at least three earlier transfers to know an account's "
            "cadence; before that they are neutral.",
        ],
    }
    return model, metadata


def write(model: Any, metadata: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = out_dir / f"{metadata['version']}.joblib"
    joblib.dump(model, artifact, compress=3)
    # The worker refuses to unpickle an artifact whose digest differs (app/ai/model.py).
    metadata["artifact_sha256"] = artifact_digest(artifact)
    meta_path = out_dir / f"{metadata['version']}.json"
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return artifact, meta_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", choices=sorted(CONFIGS), default=ACTIVE_VERSION)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out-dir", type=Path, default=MODEL_DIR)
    args = parser.parse_args()
    model, metadata = train(CONFIGS[args.version], args.seed)
    artifact, meta_path = write(model, metadata, args.out_dir)
    ev = metadata["evaluation"]
    print(f"Wrote {artifact} and {meta_path.name} (sha256 {metadata['artifact_sha256'][:12]})")
    print(f"decision {metadata['decision']}  threshold {metadata['threshold']}")
    for name in ("deployed", "model", "hybrid", "rule_baseline"):
        m = ev[name]
        print(
            f"{name:14} precision {m['precision']:.3f} recall {m['recall']:.3f} f1 {m['f1']:.3f} "
            f"fpr {m['false_positive_rate']:.4f} by kind {m['recall_by_kind']}"
        )
    print("latency", ev["inference_latency_ms_single_row_cpu"])


if __name__ == "__main__":
    main()
