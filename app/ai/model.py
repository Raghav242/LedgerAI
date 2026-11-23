"""The versioned anomaly model: artifact loading, scoring, the rule baseline.

The artifact is a scikit-learn IsolationForest saved with joblib next to `metadata.json`
(version, feature names, decision threshold, training reference statistics, evaluation).
Only the AI worker imports scikit-learn; the API reads `metadata.json` alone.

The SCORE is the negated IsolationForest `score_samples`: higher means "easier to isolate"
(more unusual relative to the training data). It is NOT a probability and NOT a fraud
likelihood. `is_anomaly` means score >= the threshold chosen on validation data.
"""

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ai.features import FEATURE_NAMES, SINCE_OFFSET, describe, vector

MODEL_DIR = Path(__file__).resolve().parent / "model"
MODEL_VERSION = "iforest-v1"
ARTIFACT = MODEL_DIR / f"{MODEL_VERSION}.joblib"
METADATA = MODEL_DIR / "metadata.json"


def load_metadata(path: Path = METADATA) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def rule_flags(features: dict[str, float]) -> list[str]:
    """The simple rule baseline the model is compared against (same features)."""
    flags = []
    if features["amount_vs_baseline"] > math.log(8):
        flags.append("amount >= 8x the account's usual amount")
    if features["log_prior_transfers"] > 0 and features["log_secs_since_prev"] < math.log(
        SINCE_OFFSET + 1.0
    ):
        flags.append("less than 1 s after the account's previous transfer")
    if features["log_prior_transfers"] == 0 and features["log_amount"] > math.log1p(500):
        flags.append("first transfer of the account above $500")
    return flags


@dataclass(frozen=True, slots=True)
class Prediction:
    model_version: str
    score: float
    threshold: float
    is_anomaly: bool
    explanation: list[str]
    rule_flags: list[str]
    inference_ms: float


class AnomalyModel:
    def __init__(self, estimator: Any, metadata: dict[str, Any]) -> None:
        if tuple(metadata["features"]) != FEATURE_NAMES:
            raise ValueError("model features do not match this code's FEATURE_NAMES")
        self._estimator = estimator
        self.metadata = metadata
        self.version: str = metadata["version"]
        self.threshold: float = metadata["threshold"]

    @classmethod
    def load(cls, artifact: Path = ARTIFACT, metadata: Path = METADATA) -> "AnomalyModel":
        import joblib  # only the AI worker image has it

        meta = load_metadata(metadata)
        if meta is None:
            raise FileNotFoundError(f"model metadata missing: {metadata}")
        return cls(joblib.load(artifact), meta)

    def score(self, features: dict[str, float]) -> float:
        import numpy as np

        return float(-self._estimator.score_samples(np.asarray([vector(features)]))[0])

    def predict(self, features: dict[str, float]) -> Prediction:
        started = time.perf_counter()
        score = self.score(features)
        elapsed = (time.perf_counter() - started) * 1000
        return Prediction(
            model_version=self.version,
            score=round(score, 6),
            threshold=self.threshold,
            is_anomaly=score >= self.threshold,
            explanation=describe(features, self.metadata["reference"]),
            rule_flags=rule_flags(features),
            inference_ms=round(elapsed, 3),
        )
