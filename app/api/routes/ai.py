"""AI anomaly API (Phase 5E): read-only views of the model and its persisted results.

The API never runs the model (it has no scikit-learn); the AI worker scores transfers
asynchronously and these routes read what it stored. Mounted with the development surface.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from app.ai.drift import MIN_SAMPLE, feature_drift
from app.ai.model import ACTIVE_VERSION, VERSIONS, load_evaluation, load_metadata
from app.api.dependencies import SessionDep
from app.api.rate_limit import ReadRateLimit
from app.core.exceptions import DomainError
from app.models import AnomalyResult
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[ReadRateLimit])


class AnomalyResultNotFoundError(DomainError):
    """No result (yet): the event may not have been scored, or the transfer does not exist."""

    code = "AI_RESULT_NOT_FOUND"


class ModelInfo(BaseModel):
    available: bool
    version: str | None = None
    versions: list[str] = []
    algorithm: str | None = None
    features: list[str] = []
    feature_schema_version: int | None = None
    families: dict[str, list[str]] | None = None
    decision: str | None = None
    decision_rule: str | None = None
    threshold: float | None = None
    model_threshold: float | None = None
    threshold_rule: str | None = None
    validation: dict[str, Any] = {}
    expected_anomaly_rate: float | None = None
    score_meaning: str | None = None
    trained_at: str | None = None
    environment: dict[str, Any] = {}
    dataset: dict[str, Any] = {}
    reference: dict[str, dict[str, Any]] = {}
    evaluation: dict[str, Any] = {}
    limitations: list[str] = []
    # Phase 6B: every version vs the rule baseline on held-out accounts, fresh seeds and
    # shifted worlds (app/ai/evaluate.py -> app/ai/model/evaluation.json).
    report: dict[str, Any] | None = None


class FeatureDriftRead(BaseModel):
    feature: str
    psi: float
    status: str
    live_median: float
    train_median: float


class DriftReport(BaseModel):
    model_version: str
    sample_size: int
    min_sample: int
    window_start: datetime | None
    window_end: datetime | None
    overall: str  # stable | moderate | significant | insufficient_data
    live_anomaly_rate: float | None
    live_model_flag_rate: float | None
    expected_anomaly_rate: float | None
    features: list[FeatureDriftRead]
    note: str


class AnomalyResultRead(BaseModel):
    event_id: str
    transfer_id: str
    source_account_id: str
    model_version: str
    score: float
    threshold: float
    is_anomaly: bool
    features: dict[str, float]
    explanation: list[str]
    rule_flags: list[str]
    inference_ms: float
    delivery_attempt: int
    occurred_at: datetime
    scored_at: datetime


def _read(r: AnomalyResult) -> AnomalyResultRead:
    return AnomalyResultRead(
        event_id=str(r.event_id),
        transfer_id=str(r.transfer_id),
        source_account_id=str(r.source_account_id),
        model_version=r.model_version,
        score=r.score,
        threshold=r.threshold,
        is_anomaly=r.is_anomaly,
        features=r.features,
        explanation=r.explanation,
        rule_flags=r.rule_flags,
        inference_ms=r.inference_ms,
        delivery_attempt=r.delivery_attempt,
        occurred_at=r.occurred_at,
        scored_at=r.scored_at,
    )


@router.get("/model", summary="The deployed model version, threshold and offline evaluation")
def model_info(request: Request) -> ModelInfo:
    meta = load_metadata()
    if meta is None:
        return ModelInfo(available=False, versions=list(VERSIONS))
    fields = {k: v for k, v in meta.items() if k in ModelInfo.model_fields}
    return ModelInfo(available=True, versions=list(VERSIONS), report=load_evaluation(), **fields)


@router.get(
    "/drift",
    summary="Input drift of recent live features vs the training distribution (PSI)",
)
def drift(
    session: SessionDep, limit: Annotated[int, Query(ge=MIN_SAMPLE, le=2000)] = 500
) -> DriftReport:
    meta = load_metadata() or {}
    rows = list(
        session.execute(
            select(
                AnomalyResult.features,
                AnomalyResult.is_anomaly,
                AnomalyResult.score,
                AnomalyResult.threshold,
                AnomalyResult.scored_at,
            )
            .where(AnomalyResult.model_version == ACTIVE_VERSION)
            .order_by(AnomalyResult.scored_at.desc())
            .limit(limit)
        )
    )
    features = feature_drift(
        [r.features for r in rows], meta.get("reference", {}), meta.get("features", [])
    )
    worst = max((f.psi for f in features), default=None)
    overall = (
        "insufficient_data"
        if len(rows) < MIN_SAMPLE or worst is None
        else "stable"
        if worst < 0.10
        else "moderate"
        if worst < 0.25
        else "significant"
    )
    n = len(rows)
    return DriftReport(
        model_version=ACTIVE_VERSION,
        sample_size=n,
        min_sample=MIN_SAMPLE,
        window_start=rows[-1].scored_at if rows else None,
        window_end=rows[0].scored_at if rows else None,
        overall=overall,
        live_anomaly_rate=round(sum(r.is_anomaly for r in rows) / n, 4) if n else None,
        live_model_flag_rate=(
            round(sum(r.score >= r.threshold for r in rows) / n, 4) if n else None
        ),
        expected_anomaly_rate=meta.get("expected_anomaly_rate"),
        features=[
            FeatureDriftRead(
                feature=f.feature,
                psi=f.psi,
                status=f.status,
                live_median=f.live_median,
                train_median=f.train_median,
            )
            for f in features
        ],
        note=(
            "PSI of the latest scored transfers against the training deciles. Lab experiments "
            "deliberately generate traffic unlike the synthetic training data, so drift is "
            "expected there; it is a signal to look, not proof the model is wrong."
        ),
    )


@router.get("/results", summary="Recent anomaly results, newest first")
def list_results(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    anomalies_only: bool = False,
    account_id: uuid.UUID | None = None,
) -> list[AnomalyResultRead]:
    query = select(AnomalyResult).order_by(AnomalyResult.scored_at.desc()).limit(limit)
    if anomalies_only:
        query = query.where(AnomalyResult.is_anomaly.is_(True))
    if account_id is not None:
        query = query.where(AnomalyResult.source_account_id == account_id)
    return [_read(r) for r in session.scalars(query)]


@router.get(
    "/results/{transfer_id}",
    summary="The anomaly result of one transfer (404 until the worker has scored it)",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
def get_result(session: SessionDep, transfer_id: uuid.UUID) -> AnomalyResultRead:
    result = session.scalar(select(AnomalyResult).where(AnomalyResult.transfer_id == transfer_id))
    if result is None:
        raise AnomalyResultNotFoundError(
            "No anomaly result for this transfer (not scored yet, or not a transfer)",
            {"transfer_id": str(transfer_id)},
        )
    return _read(result)
