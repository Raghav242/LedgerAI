"""Transactional outbox against the real PostgreSQL test database (no broker needed)."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.events.ids import event_id_for
from app.events.schemas import ACCOUNT_FUNDED, TRANSFER_COMPLETED
from app.models import OutboxEvent, TransferKind
from app.repositories.idempotency_repository import IdempotencyRepository
from app.repositories.outbox_repository import OutboxRepository
from tests.integration.support import (
    balance,
    create_account,
    ledger_entry_count,
    merchant_transfer_count,
    reconcile,
    transfer,
)

Factory = sessionmaker[Session]


def _events(factory: Factory, event_type: str | None = None) -> list[OutboxEvent]:
    with factory() as session:
        stmt = select(OutboxEvent).order_by(OutboxEvent.created_at)
        if event_type:
            stmt = stmt.where(OutboxEvent.event_type == event_type)
        return list(session.scalars(stmt))


def _claim(factory: Factory, *, batch_size: int = 100, lease_seconds: int = 30):  # type: ignore[no-untyped-def]
    token = uuid.uuid4()
    with factory() as session, session.begin():
        claimed = OutboxRepository(session).claim_batch(
            batch_size=batch_size, lease_seconds=lease_seconds, token=token
        )
    return token, claimed


# --- writing events -----------------------------------------------------------------------


def test_each_committed_transfer_writes_exactly_one_event(session_factory: Factory) -> None:
    source = create_account(session_factory, funded=10_000)
    destination = create_account(session_factory)

    result = transfer(session_factory, source, destination, 2_500)

    (event,) = _events(session_factory, TRANSFER_COMPLETED)
    assert event.id == event_id_for(TRANSFER_COMPLETED, result.transfer_id)
    assert event.aggregate_id == result.transfer_id
    assert (event.status, event.attempts, event.event_version) == ("pending", 0, 1)
    assert event.payload == {
        "transfer_id": str(result.transfer_id),
        "source_account_id": str(source),
        "destination_account_id": str(destination),
        "amount_minor": 2_500,
        "currency": "USD",
        "occurred_at": result.response_body["created_at"].replace("Z", "+00:00"),
    }


def test_funding_writes_an_account_funded_event(session_factory: Factory) -> None:
    account = create_account(session_factory, funded=4_000)
    (event,) = _events(session_factory, ACCOUNT_FUNDED)
    assert event.aggregate_id == account
    assert event.payload["amount_minor"] == 4_000


def test_rejected_transfers_write_no_events(session_factory: Factory) -> None:
    source = create_account(session_factory, funded=100)
    destination = create_account(session_factory)
    for args in [(source, destination, 101), (source, source, 1), (source, uuid.uuid4(), 1)]:
        assert not transfer(session_factory, *args).succeeded
    assert _events(session_factory, TRANSFER_COMPLETED) == []


def test_idempotent_replays_write_no_additional_events(session_factory: Factory) -> None:
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())
    for _ in range(3):
        transfer(session_factory, source, destination, 100, key=key)
    assert len(_events(session_factory, TRANSFER_COMPLETED)) == 1


def test_correlation_id_is_recorded(session_factory: Factory) -> None:
    from app.services.transfer_service import TransferCommand, TransferService

    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    with session_factory() as session:
        TransferService(session, lock_timeout_ms=5_000).create_transfer(
            scope="demo-merchant",
            idempotency_key=str(uuid.uuid4()),
            command=TransferCommand(source, destination, 10, "USD"),
            correlation_id="req-abc",
        )
    assert _events(session_factory, TRANSFER_COMPLETED)[0].correlation_id == "req-abc"


@pytest.mark.parametrize(
    ("target", "attribute"),
    [(OutboxRepository, "add"), (IdempotencyRepository, "complete")],
    ids=["outbox-insert-fails", "later-step-fails"],
)
def test_ledger_and_outbox_roll_back_together(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch, target: type, attribute: str
) -> None:
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    events_before = len(_events(session_factory))

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(target, attribute, boom)
    with pytest.raises(RuntimeError):
        transfer(session_factory, source, destination, 400)

    assert len(_events(session_factory)) == events_before  # no orphan event
    assert merchant_transfer_count(session_factory) == 0  # no orphan money movement
    assert ledger_entry_count(session_factory, kind=TransferKind.TRANSFER) == 0
    assert balance(session_factory, source) == 1_000


# --- claiming (publisher side) ------------------------------------------------------------


def test_claim_marks_rows_processing_under_a_lease(session_factory: Factory) -> None:
    create_account(session_factory, funded=100)
    create_account(session_factory, funded=200)

    token, claimed = _claim(session_factory)
    assert len(claimed) == 2
    rows = _events(session_factory)
    assert {r.status for r in rows} == {"processing"}
    assert {r.claim_token for r in rows} == {token}
    assert all(r.attempts == 1 and r.claimed_until is not None for r in rows)
    # Claimed and unexpired: nobody else can claim them.
    assert _claim(session_factory)[1] == []


def test_concurrent_publishers_claim_disjoint_batches(session_factory: Factory, engine) -> None:  # type: ignore[no-untyped-def]
    for _ in range(20):
        create_account(session_factory, funded=10)

    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(lambda _: _claim(session_factory, batch_size=7)[1], range(4)))

    ids = [e.id for batch in batches for e in batch]
    assert len(ids) == 20 and len(set(ids)) == 20  # SKIP LOCKED: no overlap, nothing missed


def test_expired_claims_are_reclaimed(session_factory: Factory) -> None:
    """A publisher crashed after claiming: its lease expires and the rows come back."""
    create_account(session_factory, funded=100)
    crashed_token, _ = _claim(session_factory)

    with session_factory() as session, session.begin():  # time passes beyond the lease
        session.execute(text("UPDATE outbox_events SET claimed_until = now() - interval '1 s'"))

    new_token, reclaimed = _claim(session_factory)
    assert len(reclaimed) == 1 and reclaimed[0].attempts == 2
    # Fencing: the crashed publisher, if it wakes up, cannot mark the row any more.
    with session_factory() as session, session.begin():
        repo = OutboxRepository(session)
        assert repo.mark_published([reclaimed[0].id], token=crashed_token) == 0
        assert repo.mark_published([reclaimed[0].id], token=new_token) == 1
    assert _events(session_factory)[0].status == "published"


def test_released_rows_back_off_before_becoming_due_again(session_factory: Factory) -> None:
    create_account(session_factory, funded=100)
    token, (event,) = _claim(session_factory)
    with session_factory() as session, session.begin():
        released = OutboxRepository(session).release_for_retry(
            [event.id], token=token, error="broker down", base_ms=60_000, cap_ms=60_000
        )
    assert released == 1
    row = _events(session_factory)[0]
    assert (row.status, row.last_error, row.claim_token) == ("pending", "broker down", None)
    with session_factory() as session:
        delay = session.scalar(
            text("SELECT extract(epoch FROM available_at - now()) FROM outbox_events")
        )
    assert 25 <= float(delay) <= 60  # equal jitter: [30 s, 60 s] minus elapsed time
    assert _claim(session_factory)[1] == []  # not due yet


def test_published_rows_are_never_claimed_again(session_factory: Factory) -> None:
    create_account(session_factory, funded=100)
    token, (event,) = _claim(session_factory)
    with session_factory() as session, session.begin():
        OutboxRepository(session).mark_published([event.id], token=token)
    with session_factory() as session, session.begin():
        session.execute(text("UPDATE outbox_events SET claimed_until = now() - interval '1 h'"))
    assert _claim(session_factory)[1] == []


def test_poll_is_served_by_a_partial_index_on_unpublished_rows(session_factory: Factory) -> None:
    with session_factory() as session:
        indexdef = session.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_outbox_events_unpublished'")
        )
    assert "(available_at)" in indexdef
    assert "WHERE" in indexdef and "'published'" in indexdef


def test_reconciliation_still_balances_with_outbox_writes(session_factory: Factory) -> None:
    a = create_account(session_factory, funded=5_000)
    b = create_account(session_factory)
    for _ in range(3):
        transfer(session_factory, a, b, 100)
    assert reconcile(session_factory).ok
    assert len(_events(session_factory)) == 4  # 1 funding + 3 transfers
