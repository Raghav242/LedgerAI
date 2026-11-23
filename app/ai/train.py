"""Train and evaluate the anomaly model on deterministic synthetic data.

    python -m app.ai.train            # writes app/ai/model/iforest-v1.joblib + metadata.json

Pipeline (all seeded, so the same command reproduces the same model and numbers):
  1. generate synthetic activity and replay it into features (no future information)
  2. split 60/20/20 BY ACCOUNT into train / validation / test
  3. fit an IsolationForest on the TRAINING rows only (unsupervised: labels are not used)
  4. pick the decision threshold that maximises F1 on the VALIDATION rows
  5. report precision, recall, F1 and false-positive rate on the held-out TEST rows,
     for the model and for the rule baseline, plus single-row CPU inference latency
"""

import json
import platform
import statistics
import time
from datetime import UTC, datetime
from typing import Any

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest

from app.ai.features import FEATURE_NAMES, vector
from app.ai.model import ARTIFACT, METADATA, MODEL_DIR, MODEL_VERSION, rule_flags
from app.ai.synthetic import DEFAULT_SEED, Row, featurize, generate, split_by_account

N_ESTIMATORS = 200
MAX_SAMPLES = 256


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
    for kind in ("large", "burst", "new_account"):
        hits = [p for p, r in zip(predicted, rows, strict=True) if r.label == kind]
        out[kind] = round(sum(hits) / len(hits), 4) if hits else 0.0
    return out


def matrix(rows: list[Row]) -> np.ndarray:
    return np.asarray([vector(r.features) for r in rows])


def best_threshold(scores: np.ndarray, actual: list[bool]) -> float:
    best = (-1.0, float(np.quantile(scores, 0.97)))
    for q in np.linspace(0.80, 0.999, 400):
        t = float(np.quantile(scores, q))
        f1 = confusion([s >= t for s in scores], actual)["f1"]
        if f1 > best[0]:
            best = (f1, t)
    return best[1]


def reference_stats(rows: list[Row]) -> dict[str, dict[str, float]]:
    x = matrix(rows)
    return {
        name: {
            "p05": round(float(np.quantile(x[:, i], 0.05)), 4),
            "median": round(float(np.median(x[:, i])), 4),
            "p95": round(float(np.quantile(x[:, i], 0.95)), 4),
        }
        for i, name in enumerate(FEATURE_NAMES)
    }


def inference_latency_ms(model: IsolationForest, rows: list[Row], n: int = 300) -> dict[str, float]:
    samples = []
    for row in rows[:n]:
        x = np.asarray([vector(row.features)])
        started = time.perf_counter()
        model.score_samples(x)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return {
        "p50": round(statistics.median(samples), 3),
        "p95": round(samples[int(len(samples) * 0.95) - 1], 3),
        "samples": len(samples),
    }


def train(seed: int = DEFAULT_SEED) -> tuple[IsolationForest, dict[str, Any]]:
    transfers, created = generate(seed)
    rows = featurize(transfers, created)
    train_rows, val_rows, test_rows = split_by_account(rows, seed)
    model = IsolationForest(
        n_estimators=N_ESTIMATORS, max_samples=MAX_SAMPLES, contamination="auto", random_state=seed
    )
    model.fit(matrix(train_rows))  # labels are never shown to the model

    val_scores = -model.score_samples(matrix(val_rows))
    threshold = best_threshold(val_scores, [r.anomalous for r in val_rows])
    test_scores = -model.score_samples(matrix(test_rows))
    actual = [r.anomalous for r in test_rows]
    predicted = [bool(s >= threshold) for s in test_scores]
    rules = [bool(rule_flags(r.features)) for r in test_rows]

    def counts(rs: list[Row]) -> dict[str, int]:
        return {
            k: sum(1 for r in rs if r.label == k)
            for k in ("normal", "large", "burst", "new_account")
        }

    metadata = {
        "version": MODEL_VERSION,
        "algorithm": (
            f"scikit-learn IsolationForest (n_estimators={N_ESTIMATORS}, max_samples={MAX_SAMPLES})"
        ),
        "features": list(FEATURE_NAMES),
        "threshold": round(threshold, 6),
        "threshold_rule": "maximises F1 on the validation split",
        "score_meaning": (
            "negated IsolationForest score_samples; higher = more unusual. Not a probability."
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
        "reference": reference_stats(train_rows),
        "evaluation": {
            "split": "held-out test accounts (never seen in training or threshold selection)",
            "model": {
                **confusion(predicted, actual),
                "recall_by_kind": recall_by_kind(predicted, test_rows),
            },
            "rule_baseline": {
                **confusion(rules, actual),
                "recall_by_kind": recall_by_kind(rules, test_rows),
            },
            "inference_latency_ms_single_row_cpu": inference_latency_ms(model, test_rows),
        },
        "limitations": [
            "Trained and evaluated on synthetic data only; says nothing about real-world fraud.",
            "Labels are the generator's intent; the model never sees them during training.",
            "Compressed clock: seconds stand in for hours or days so lab scenarios run live.",
            "Only the source account's own history is used (no counterparties, devices, "
            "geography).",
            "The score is relative unusualness, not a probability.",
        ],
    }
    return model, metadata


def main() -> None:
    model, metadata = train()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, ARTIFACT, compress=3)
    METADATA.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    ev = metadata["evaluation"]
    print(f"Wrote {ARTIFACT.name} and {METADATA.name}")
    print(f"threshold {metadata['threshold']}  rows {metadata['dataset']['rows']}")
    for name in ("model", "rule_baseline"):
        m = ev[name]
        print(
            f"{name:14} precision {m['precision']:.3f} recall {m['recall']:.3f} f1 {m['f1']:.3f} "
            f"fpr {m['false_positive_rate']:.4f} by kind {m['recall_by_kind']}"
        )
    print("latency", ev["inference_latency_ms_single_row_cpu"])


if __name__ == "__main__":
    main()
