"""Offline evaluation of every model version against the rule baseline (Phase 6B).

    python -m app.ai.evaluate          # writes app/ai/model/evaluation.json

Detectors compared (all on identical rows):
  v1_model     iforest-v1 (Phase 5E): one forest, six features, its own threshold
  rules        the hand-written rule baseline (app/ai/model.py rule_flags)
  v2_model     iforest-v2 alone: per-family forests with velocity features, its own threshold
  v2_hybrid    iforest-v2 as deployed: score >= deployed threshold OR any rule flag

Datasets (synthetic; nothing here is real-world performance):
  held_out_accounts   the test split of the training seed (accounts never seen in training
                      or threshold selection)
  fresh_seed_*        three entirely new worlds (other seeds); every account is unseen.
                      Reported individually and as mean +/- standard deviation
  shift_*             distribution shifts the models were not trained on (app/ai/synthetic.py
                      SCENARIOS): amounts x3, faster normal accounts, slower bursts

Thresholds come from the models' metadata (chosen on validation data at training time);
nothing is tuned here. Deterministic: same code + same artifacts = same report.
"""

import json
import platform
import statistics
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
import sklearn

from app.ai.model import EVALUATION_REPORT, AnomalyModel, rule_flags
from app.ai.synthetic import DEFAULT_SEED, SCENARIOS, Row, featurize, generate, split_by_account
from app.ai.train import KINDS, metrics

FRESH_SEEDS = (101, 202, 303)
SHIFT_SEED = 404
ACCOUNTS = 600  # per evaluation world (the training world has 1200)
DETECTORS = ("v1_model", "rules", "v2_model", "v2_hybrid")
SUMMARY_KEYS = ("precision", "recall", "f1", "false_positive_rate")


def predictions(v1: AnomalyModel, v2: AnomalyModel, rows: list[Row]) -> dict[str, list[bool]]:
    features = [r.features for r in rows]
    s1 = v1.score_many(features)
    s2 = v2.score_many(features)
    rules = [bool(rule_flags(f)) for f in features]
    model_threshold = v2.metadata.get("model_threshold", v2.threshold)
    return {
        "v1_model": [bool(s >= v1.threshold) for s in s1],
        "rules": rules,
        "v2_model": [bool(s >= model_threshold) for s in s2],
        "v2_hybrid": [
            v2.decide(float(s), ["x"] if r else []) for s, r in zip(s2, rules, strict=True)
        ],
    }


def evaluate_rows(v1: AnomalyModel, v2: AnomalyModel, rows: list[Row]) -> dict[str, Any]:
    preds = predictions(v1, v2, rows)
    return {
        "rows": len(rows),
        "anomalies": {k: sum(1 for r in rows if r.label == k) for k in KINDS},
        "detectors": {name: metrics(p, rows) for name, p in preds.items()},
    }


def disagreements(v2: AnomalyModel, rows: list[Row]) -> dict[str, Any]:
    """Where the deployed model and the rules disagree (v2 at its deployed threshold)."""
    scores = v2.score_many([r.features for r in rows])
    out: dict[str, dict[str, int]] = {"model_only": {}, "rules_only": {}, "both": {}}
    for s, r in zip(scores, rows, strict=True):
        m, q = bool(s >= v2.threshold), bool(rule_flags(r.features))
        bucket = "both" if m and q else "model_only" if m else "rules_only" if q else None
        if bucket:
            out[bucket][r.label] = out[bucket].get(r.label, 0) + 1
    return out


def aggregate(worlds: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in DETECTORS:
        runs = [w["detectors"][name] for w in worlds]
        summary = {}
        for key in SUMMARY_KEYS:
            values = [r[key] for r in runs]
            summary[key] = {
                "mean": round(statistics.mean(values), 4),
                "std": round(statistics.pstdev(values), 4),
            }
        summary["recall_by_kind"] = {
            kind: {
                "mean": round(statistics.mean(r["recall_by_kind"][kind] for r in runs), 4),
                "std": round(statistics.pstdev(r["recall_by_kind"][kind] for r in runs), 4),
            }
            for kind in KINDS
        }
        out[name] = summary
    return out


def latency(model: AnomalyModel, rows: list[Row], n: int = 300) -> dict[str, float]:
    samples = []
    for r in rows[:n]:
        started = time.perf_counter()
        model.predict(r.features)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    return {
        "p50": round(statistics.median(samples), 3),
        "p95": round(samples[int(len(samples) * 0.95) - 1], 3),
        "samples": len(samples),
    }


def run() -> dict[str, Any]:
    v1 = AnomalyModel.load_version("iforest-v1")
    v2 = AnomalyModel.load_version("iforest-v2")
    rows = featurize(*generate(DEFAULT_SEED))
    _, _, held_out = split_by_account(rows, DEFAULT_SEED)

    datasets: dict[str, Any] = {"held_out_accounts": evaluate_rows(v1, v2, held_out)}
    fresh = []
    for seed in FRESH_SEEDS:
        world = featurize(*generate(seed, accounts=ACCOUNTS))
        result = evaluate_rows(v1, v2, world)
        datasets[f"fresh_seed_{seed}"] = result
        fresh.append(result)
    for name, scenario in SCENARIOS.items():
        if name == "baseline":
            continue
        world = featurize(*generate(SHIFT_SEED, accounts=ACCOUNTS, scenario=scenario))
        datasets[f"shift_{name}"] = {
            **evaluate_rows(v1, v2, world),
            "scenario": scenario.description,
        }
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": {
            "python": platform.python_version(),
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
        },
        "models": {
            m.version: {
                "artifact_sha256": m.metadata["artifact_sha256"],
                "threshold": m.threshold,
                "decision": m.decision,
                "features": list(m.features),
            }
            for m in (v1, v2)
        },
        "detectors": {
            "v1_model": "iforest-v1 alone (Phase 5E baseline)",
            "rules": "rule baseline (amount >= 8x usual, < 1 s after previous, big first transfer)",
            "v2_model": "iforest-v2 alone (per-family forests + velocity features)",
            "v2_hybrid": f"iforest-v2 as deployed ({v2.decision})",
        },
        "datasets": datasets,
        "fresh_seeds_summary": aggregate(fresh),
        "disagreements_held_out": disagreements(v2, held_out),
        "inference_latency_ms_single_row_cpu": {
            "iforest-v1": latency(v1, held_out),
            "iforest-v2": latency(v2, held_out),
        },
        "notes": [
            "Synthetic data only; the rule baseline was written against the same generator, "
            "which is why it is strong in-distribution.",
            "Thresholds were chosen on validation data at training time; nothing is tuned on "
            "these datasets.",
            "Per-scenario recall: the share of each injected anomaly kind that was flagged.",
        ],
    }


def main() -> None:
    report = run()
    EVALUATION_REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {EVALUATION_REPORT}")
    for name, ds in report["datasets"].items():
        print(f"\n{name} ({ds['rows']} rows, anomalies {ds['anomalies']})")
        for det, m in ds["detectors"].items():
            print(
                f"  {det:10} P {m['precision']:.3f} R {m['recall']:.3f} F1 {m['f1']:.3f} "
                f"FPR {m['false_positive_rate']:.4f} {m['recall_by_kind']}"
            )
    print("\nfresh seeds mean F1:", {d: s["f1"] for d, s in report["fresh_seeds_summary"].items()})
    print("disagreements:", report["disagreements_held_out"])
    print("latency:", report["inference_latency_ms_single_row_cpu"])


if __name__ == "__main__":
    main()
