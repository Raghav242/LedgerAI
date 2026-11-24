"""The AI anomaly worker: scores transfer.completed events asynchronously (Phase 5E).

Run:  python -m app.ai.worker      (the `ai-worker` Compose service)

It is one more consumer of the event pipeline, built from the same EventConsumer as the
audit consumer (manual acks, bounded retries, DLQ, heartbeat, trace propagation), with its
own queue `ledgerflow.anomaly-detector`. Per delivery, ONE transaction:

    INSERT processed_events (event_id, 'anomaly-detector')  ON CONFLICT DO NOTHING
      -> conflict: duplicate delivery, done (no second result)
      -> new:      features from the source account's EARLIER transfers (SQL aggregates)
                   -> model prediction -> INSERT anomaly_results
    COMMIT, then ack

Transfers never wait for this: the API commits and answers; scoring happens later.
account.funded events are acknowledged with a receipt and no result (nothing to score).
"""

import logging
import signal
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.ai.features import (
    BURST_WINDOW_SECONDS,
    HISTORY_WINDOW,
    MIN_GAPS_FOR_CADENCE,
    SHORT_WINDOW_SECONDS,
    FeatureSchemaError,
    PriorStats,
    compute_features,
)
from app.ai.model import AnomalyModel
from app.core.config import Settings, get_settings
from app.core.database import atomic, create_db_engine, create_session_factory
from app.core.logging import configure_logging
from app.events.consumer import EventConsumer, Outcome, PermanentEventError
from app.events.schemas import TRANSFER_COMPLETED, EventEnvelope, TransferCompletedV1
from app.models import Account, AnomalyResult, ProcessedEvent, Transfer, TransferKind
from app.observability.heartbeats import HeartbeatReporter
from app.observability.tracing import configure_tracing, instrument_worker_engine, tracer

logger = logging.getLogger(__name__)

CONSUMER = "anomaly-detector"


def prior_stats(session: Session, source: uuid.UUID, at: datetime) -> PriorStats:
    """The same PriorStats the training replay computes, from PostgreSQL, strictly before `at`."""
    earlier = select(Transfer.amount_minor, Transfer.created_at).where(
        Transfer.source_account_id == source,
        Transfer.kind == TransferKind.TRANSFER,
        Transfer.created_at < at,
    )
    history = earlier.subquery()
    # Aggregate the SUBQUERY's columns only (never the base table: that would be a join).
    count, last_at = session.execute(
        select(func.count(), func.max(history.c.created_at)).select_from(history)
    ).one()
    recent_window = earlier.where(
        Transfer.created_at >= at - timedelta(seconds=BURST_WINDOW_SECONDS)
    ).subquery()
    recent = session.scalar(select(func.count()).select_from(recent_window))
    short_window = earlier.where(
        Transfer.created_at >= at - timedelta(seconds=SHORT_WINDOW_SECONDS)
    ).subquery()
    short = session.scalar(select(func.count()).select_from(short_window))
    window = earlier.order_by(Transfer.created_at.desc()).limit(HISTORY_WINDOW).subquery()
    median = session.scalar(select(func.percentile_cont(0.5).within_group(window.c.amount_minor)))
    # Schema v2: median gap (seconds) between consecutive transfers in the same window.
    gap = (
        func.extract("epoch", window.c.created_at)
        - func.extract("epoch", func.lag(window.c.created_at).over(order_by=window.c.created_at))
    ).label("gap")
    gaps = select(gap).select_from(window).subquery()
    n_gaps, median_gap = session.execute(
        select(func.count(gaps.c.gap), func.percentile_cont(0.5).within_group(gaps.c.gap))
    ).one()
    return PriorStats(
        count=count or 0,
        median_amount_minor=float(median) if median is not None else None,
        last_at=last_at,
        count_last_60s=recent or 0,
        median_gap_s=(
            float(median_gap) if median_gap is not None and n_gaps >= MIN_GAPS_FOR_CADENCE else None
        ),
        count_last_10s=short or 0,
    )


class AnomalyHandler:
    def __init__(self, session_factory: sessionmaker[Session], model: AnomalyModel) -> None:
        self._sessions = session_factory
        self._model = model

    def handle(
        self, event: EventEnvelope, *, delivery_attempt: int, redelivered: bool = False
    ) -> Outcome:
        try:
            with self._sessions() as session, atomic(session):
                receipt = session.execute(
                    insert(ProcessedEvent)
                    .values(event_id=event.event_id, consumer_name=CONSUMER)
                    .on_conflict_do_nothing(index_elements=["event_id", "consumer_name"])
                    .returning(ProcessedEvent.event_id)
                ).first()
                if receipt is None:
                    return Outcome.DUPLICATE
                if event.event_type == TRANSFER_COMPLETED:
                    self._score(session, event, delivery_attempt)
        except IntegrityError as exc:
            raise PermanentEventError(f"constraint violation: {exc.orig}") from exc
        return Outcome.PROCESSED

    def _score(self, session: Session, event: EventEnvelope, delivery_attempt: int) -> None:
        payload = TransferCompletedV1.model_validate(event.payload)
        account = session.get(Account, payload.source_account_id)
        if account is None:
            raise PermanentEventError(f"unknown source account {payload.source_account_id}")
        with tracer.start_as_current_span("ai.features"):
            prior = prior_stats(session, payload.source_account_id, payload.occurred_at)
            features = compute_features(
                amount_minor=payload.amount_minor,
                at=payload.occurred_at,
                account_created_at=account.created_at,
                prior=prior,
            )
        with tracer.start_as_current_span("ai.predict") as span:
            try:
                prediction = self._model.predict(features)
            except FeatureSchemaError as exc:  # never score malformed inputs; DLQ, no retry
                raise PermanentEventError(f"feature schema: {exc}") from exc
            span.set_attribute("ledgerflow.ai.score", prediction.score)
            span.set_attribute("ledgerflow.ai.is_anomaly", prediction.is_anomaly)
            span.set_attribute("ledgerflow.ai.model_version", prediction.model_version)
        session.add(
            AnomalyResult(
                event_id=event.event_id,
                transfer_id=payload.transfer_id,
                source_account_id=payload.source_account_id,
                model_version=prediction.model_version,
                score=prediction.score,
                threshold=prediction.threshold,
                is_anomaly=prediction.is_anomaly,
                features={k: round(v, 6) for k, v in features.items()},
                explanation=prediction.explanation,
                rule_flags=prediction.rule_flags,
                inference_ms=prediction.inference_ms,
                delivery_attempt=delivery_attempt,
                occurred_at=payload.occurred_at,
            )
        )
        session.flush()


def build_worker(settings: Settings) -> tuple[EventConsumer, object]:
    settings = settings.model_copy(update={"consumer_name": CONSUMER})
    tracing = configure_tracing(settings, service_name="ledgerflow-ai-worker")
    engine = create_db_engine(settings)
    if tracing:
        instrument_worker_engine(engine)
    sessions = create_session_factory(engine)
    model = AnomalyModel.load()
    logger.info("Loaded model %s (threshold %.4f)", model.version, model.threshold)
    heartbeat = HeartbeatReporter(
        sessions,
        component="anomaly-detector",
        instance=settings.replica_id,
        interval_seconds=settings.heartbeat_interval_seconds,
    )
    return EventConsumer(settings, AnomalyHandler(sessions, model), heartbeat=heartbeat), engine


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    consumer, engine = build_worker(settings)
    try:
        consumer.run_forever(stop)
    finally:
        engine.dispose()  # type: ignore[attr-defined]
        logger.info("AI worker stopped: %s", dict(consumer.counts))


if __name__ == "__main__":
    main()
