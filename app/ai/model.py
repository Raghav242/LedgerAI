"""The versioned anomaly model: artifact loading, scoring, the rule baseline.

The artifact is a scikit-learn IsolationForest saved with joblib next to `metadata.json`
(version, feature names, decision threshold, training reference statistics, evaluation).
Only the AI worker imports scikit-learn; the API reads `metadata.json` alone.

Safe loading (Phase 6A): a joblib file is a pickle, and unpickling runs code. The worker
therefore refuses to load the artifact unless its SHA-256 matches `artifact_sha256` in
the metadata written by app/ai/train.py, and unless the installed scikit-learn is the
exact version that trained it. This catches a swapped, corrupted or stale artifact; it is
an integrity check, not a signature (whoever can change both files can change both).

The SCORE is the negated IsolationForest `score_samples`: higher means "easier to isolate"
(more unusual relative to the training data). It is NOT a probability and NOT a fraud
likelihood. `is_anomaly` means score >= the threshold chosen on validation data.
"""

import hashlib
import hmac
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ai.features import (
    FEATURE_NAMES,
    SINCE_OFFSET,
    describe,
    validate_features,
    vector,
)

# Versioned model registry (Phase 6B): app/ai/model/<version>.joblib + <version>.json.
# Every version stays loadable (and is re-evaluated by app/ai/evaluate.py); the worker
# serves ACTIVE_VERSION. A new version is a new pair of files, never an overwrite.
MODEL_DIR = Path(__file__).resolve().parent / "model"
VERSIONS = ("iforest-v1", "iforest-v2")
MODEL_VERSION = ACTIVE_VERSION = "iforest-v2"
EVALUATION_REPORT = MODEL_DIR / "evaluation.json"
Decision = str  # "model": score >= threshold; "hybrid": model OR rule baseline


def artifact_path(version: str) -> Path:
    return MODEL_DIR / f"{version}.joblib"


def metadata_path(version: str) -> Path:
    return MODEL_DIR / f"{version}.json"


ARTIFACT = artifact_path(ACTIVE_VERSION)
METADATA = metadata_path(ACTIVE_VERSION)


class ModelIntegrityError(RuntimeError):
    """The artifact does not match its metadata; it is never unpickled."""


def artifact_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(artifact: Path, metadata: dict[str, Any], sklearn_version: str) -> None:
    """Raise ModelIntegrityError unless `artifact` is exactly what `metadata` describes."""
    expected = metadata.get("artifact_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ModelIntegrityError(
            "model metadata has no artifact_sha256; refusing to unpickle an unverified artifact"
        )
    actual = artifact_digest(artifact)
    if not hmac.compare_digest(actual, expected.lower()):
        raise ModelIntegrityError(
            f"model artifact {artifact.name} SHA-256 {actual[:12]}... does not match the "
            f"metadata ({expected[:12]}...); refusing to load it"
        )
    trained_with = (metadata.get("environment") or {}).get("scikit_learn")
    if trained_with != sklearn_version:
        raise ModelIntegrityError(
            f"model was trained with scikit-learn {trained_with}, installed is "
            f"{sklearn_version}; pin the trained version (requirements-ai.lock)"
        )


def load_evaluation(path: Path = EVALUATION_REPORT) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


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
    # Phase 6B: the model's own verdict and how the final one was made. With the hybrid
    # decision, is_anomaly = model_flag OR any rule flag; disagreements stay visible.
    model_flag: bool = False
    decision: Decision = "model"


class AnomalyModel:
    def __init__(self, estimator: Any, metadata: dict[str, Any]) -> None:
        names = tuple(metadata["features"])
        unknown = [n for n in names if n not in FEATURE_NAMES]
        if unknown:
            raise ValueError(f"model expects features this code cannot compute: {unknown}")
        self._estimator = estimator
        self.metadata = metadata
        self.features: tuple[str, ...] = names
        self.version: str = metadata["version"]
        self.threshold: float = metadata["threshold"]
        self.decision: Decision = metadata.get("decision", "model")
        if self.decision not in ("model", "hybrid"):
            raise ValueError(f"unknown decision policy {self.decision!r}")

    @classmethod
    def load(cls, artifact: Path = ARTIFACT, metadata: Path = METADATA) -> "AnomalyModel":
        import joblib  # only the AI worker image has it
        import sklearn

        meta = load_metadata(metadata)
        if meta is None:
            raise FileNotFoundError(f"model metadata missing: {metadata}")
        verify_artifact(artifact, meta, sklearn.__version__)
        return cls(joblib.load(artifact), meta)

    @classmethod
    def load_version(cls, version: str) -> "AnomalyModel":
        if version not in VERSIONS:
            raise ValueError(f"unknown model version {version!r}")
        return cls.load(artifact_path(version), metadata_path(version))

    def _unusualness(self, x: Any) -> Any:
        # v2: GroupedIsolationForest (app/ai/ensemble.py); v1: a plain IsolationForest.
        if hasattr(self._estimator, "anomaly_score"):
            return self._estimator.anomaly_score(x)
        return -self._estimator.score_samples(x)

    def score(self, features: dict[str, float]) -> float:
        import numpy as np

        validate_features(features, self.features)
        return float(self._unusualness(np.asarray([vector(features, self.features)]))[0])

    def score_many(self, rows: list[dict[str, float]]) -> Any:
        """Batch scores (evaluation); same schema checks as single-row scoring."""
        import numpy as np

        for features in rows:
            validate_features(features, self.features)
        return self._unusualness(np.asarray([vector(f, self.features) for f in rows]))

    def decide(self, score: float, flags: list[str]) -> bool:
        model_flag = score >= self.threshold
        return model_flag or (self.decision == "hybrid" and bool(flags))

    def predict(self, features: dict[str, float]) -> Prediction:
        started = time.perf_counter()
        score = self.score(features)
        flags = rule_flags(features)
        elapsed = (time.perf_counter() - started) * 1000
        return Prediction(
            model_version=self.version,
            score=round(score, 6),
            threshold=self.threshold,
            is_anomaly=self.decide(score, flags),
            explanation=describe(features, self.metadata["reference"]),
            rule_flags=flags,
            inference_ms=round(elapsed, 3),
            model_flag=score >= self.threshold,
            decision=self.decision,
        )
