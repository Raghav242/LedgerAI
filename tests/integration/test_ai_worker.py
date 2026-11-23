"""AI worker against the real PostgreSQL test database: SQL/replay feature parity,
exactly-once results under redelivery, the read-only AI API, and that the API process
never loads the model (transfers can never wait for inference)."""

import subprocess
import sys
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.features import prior_from_history
from app.ai.model import AnomalyModel
from app.ai.worker import CONSUMER, AnomalyHandler, prior_stats
from app.events.consumer import Outcome
from app.events.ids import event_id_for
from app.events.schemas import ACCOUNT_FUNDED, TRANSFER_COMPLETED, EventEnvelope
from app.models import AnomalyResult, OutboxEvent, ProcessedEvent, Transfer, TransferKind
from tests.integration.support import create_account, transfer

Factory = sessionmaker[Session]


@pytest.fixture(scope="module")
def model() -> AnomalyModel:
    return AnomalyModel.load()


def _envelope(factory: Factory, event_id: uuid.UUID) -> EventEnvelope:
    with factory() as session:
        row = session.get_one(OutboxEvent, event_id)
        return EventEnvelope(
            event_id=row.id,
            event_type=row.event_type,
            event_version=row.event_version,
            aggregate_id=row.aggregate_id,
            occurred_at=row.created_at,
            correlation_id=row.correlation_id,
            payload=row.payload,
        )


def _history(factory: Factory, source: uuid.UUID) -> list:
    with factory() as session:
        rows = session.execute(
            select(Transfer.created_at, Transfer.amount_minor).where(
                Transfer.source_account_id == source, Transfer.kind == TransferKind.TRANSFER
            )
        ).all()
    return [(t, a) for t, a in rows]


def test_sql_features_equal_the_training_replay(session_factory: Factory) -> None:
    source = create_account(session_factory, funded=100_000)
    destination = create_account(session_factory)
    for amount in (1_000, 2_500, 1_700, 4_200, 900):
        transfer(session_factory, source, destination, amount)
    history = _history(session_factory, source)
    times = sorted(t for t, _ in history)
    # At several cut points, including exactly on an existing transfer's timestamp.
    for at in (times[2], times[-1] + timedelta(microseconds=1), times[0] - timedelta(seconds=1)):
        with session_factory() as session:
            sql = prior_stats(session, source, at)
        assert sql == prior_from_history(history, at), at


def test_scores_once_despite_redelivery(session_factory: Factory, model: AnomalyModel) -> None:
    source = create_account(session_factory, funded=50_000)
    destination = create_account(session_factory)
    result = transfer(session_factory, source, destination, 3_000)
    assert result.transfer_id is not None
    event = _envelope(session_factory, event_id_for(TRANSFER_COMPLETED, result.transfer_id))
    handler = AnomalyHandler(session_factory, model)

    assert handler.handle(event, delivery_attempt=1) == Outcome.PROCESSED
    assert handler.handle(event, delivery_attempt=2, redelivered=True) == Outcome.DUPLICATE
    with session_factory() as session:
        results = session.scalars(
            select(AnomalyResult).where(AnomalyResult.transfer_id == result.transfer_id)
        ).all()
        receipts = session.scalar(
            select(func.count())
            .select_from(ProcessedEvent)
            .where(
                ProcessedEvent.event_id == event.event_id, ProcessedEvent.consumer_name == CONSUMER
            )
        )
    assert len(results) == 1 and receipts == 1
    stored = results[0]
    assert stored.model_version == model.version and stored.delivery_attempt == 1
    assert stored.is_anomaly == (stored.score >= stored.threshold)
    # First transfer of a brand-new account: no earlier history was used.
    assert stored.features["log_prior_transfers"] == 0.0


def test_funding_events_get_a_receipt_but_no_result(
    session_factory: Factory, model: AnomalyModel
) -> None:
    account = create_account(session_factory, funded=1_000)
    with session_factory() as session:
        event_id = session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.event_type == ACCOUNT_FUNDED, OutboxEvent.aggregate_id == account
            )
        )
    assert event_id is not None
    outcome = AnomalyHandler(session_factory, model).handle(
        _envelope(session_factory, event_id), delivery_attempt=1
    )
    assert outcome == Outcome.PROCESSED
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AnomalyResult)) == 0


def test_ai_api_is_read_only_and_reports_pending_results(
    client: TestClient, session_factory: Factory, model: AnomalyModel
) -> None:
    info = client.get("/api/v1/ai/model").json()
    assert info["available"] and info["version"] == "iforest-v1"
    assert info["evaluation"]["model"]["f1"] > 0
    source = create_account(session_factory, funded=50_000)
    result = transfer(session_factory, source, create_account(session_factory), 2_000)
    pending = client.get(f"/api/v1/ai/results/{result.transfer_id}")
    assert pending.status_code == 404 and pending.json()["error"]["code"] == "AI_RESULT_NOT_FOUND"
    event = _envelope(session_factory, event_id_for(TRANSFER_COMPLETED, result.transfer_id))
    AnomalyHandler(session_factory, model).handle(event, delivery_attempt=1)
    scored = client.get(f"/api/v1/ai/results/{result.transfer_id}").json()
    assert scored["transfer_id"] == str(result.transfer_id) and "score" in scored
    assert [r["transfer_id"] for r in client.get("/api/v1/ai/results").json()] == [
        str(result.transfer_id)
    ]
    paths = client.get("/openapi.json").json()["paths"]
    assert all(set(ops) == {"get"} for p, ops in paths.items() if p.startswith("/api/v1/ai"))


def test_the_api_process_never_loads_the_model() -> None:
    """Transfers cannot wait for inference: the API does not even import scikit-learn."""
    code = (
        "import sys; from app.main import create_app; create_app(); "
        "print('sklearn' in sys.modules, 'joblib' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip().splitlines()[-1] == "False False"
