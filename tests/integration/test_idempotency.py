import threading
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import IdempotencyKeyReusedError, IdempotencyRequestInProgressError
from app.models import IdempotencyRecord, TransferKind
from app.services.ledger_posting import LedgerPoster
from tests.integration.conftest import create_account as api_create_account
from tests.integration.conftest import fund_account as api_fund_account
from tests.integration.conftest import get_balance, post_transfer
from tests.integration.support import (
    balance,
    create_account,
    idempotency_record,
    merchant_transfer_count,
    run_concurrently,
    transfer,
    wait_for_lock_waiters,
)

Factory = sessionmaker[Session]


# --- service level ---------------------------------------------------------------------


def test_concurrent_requests_with_same_key_move_money_once(concurrent_factory: Factory) -> None:
    """Requirement 2: 25 simultaneous identical requests, one key -> one transfer."""
    source = create_account(concurrent_factory, funded=10_000)
    destination = create_account(concurrent_factory)
    key = str(uuid.uuid4())

    results = run_concurrently(
        25, lambda _: transfer(concurrent_factory, source, destination, 1_000, key=key)
    )

    assert [r for r in results if isinstance(r, BaseException)] == []
    originals = [r for r in results if not r.replayed]
    assert len(originals) == 1
    assert all(r.succeeded for r in results)
    assert {r.transfer_id for r in results} == {originals[0].transfer_id}
    assert all(r.response_body == originals[0].response_body for r in results)
    assert balance(concurrent_factory, source) == 9_000
    assert merchant_transfer_count(concurrent_factory) == 1
    with concurrent_factory() as session:
        assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


def test_same_key_with_different_payload_is_rejected(session_factory: Factory) -> None:
    """Requirement 3."""
    source = create_account(session_factory, funded=10_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())
    transfer(session_factory, source, destination, 1_000, key=key)

    with pytest.raises(IdempotencyKeyReusedError):
        transfer(session_factory, source, destination, 2_000, key=key)
    with pytest.raises(IdempotencyKeyReusedError):
        transfer(session_factory, destination, source, 1_000, key=key)

    assert balance(session_factory, source) == 9_000
    assert merchant_transfer_count(session_factory) == 1


def test_keys_are_isolated_per_scope(session_factory: Factory) -> None:
    source = create_account(session_factory, funded=10_000)
    destination = create_account(session_factory)

    first = transfer(session_factory, source, destination, 100, key="order-1", scope="merchant-a")
    second = transfer(session_factory, source, destination, 100, key="order-1", scope="merchant-b")

    assert not first.replayed and not second.replayed
    assert first.transfer_id != second.transfer_id
    assert merchant_transfer_count(session_factory) == 2


@pytest.fixture
def paused_poster(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[threading.Event, threading.Event]]:
    """Pause the first merchant-transfer LedgerPoster.post call: the request has claimed
    its key (and holds the unique-index lock) but has not committed.

    Yields (claimed, release) events.
    """
    claimed, release = threading.Event(), threading.Event()
    real_post = LedgerPoster.post
    calls = {"n": 0}

    def paused(self: LedgerPoster, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs["kind"] != TransferKind.TRANSFER:  # e.g. test-setup funding
            return real_post(self, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            claimed.set()
            assert release.wait(30)
        return real_post(self, **kwargs)

    monkeypatch.setattr(LedgerPoster, "post", paused)
    yield claimed, release
    release.set()


def _in_background(fn):  # type: ignore[no-untyped-def]
    box: dict[str, object] = {}
    thread = threading.Thread(target=lambda: box.__setitem__("result", _capture(fn)))
    thread.start()
    return thread, box


def _capture(fn):  # type: ignore[no-untyped-def]
    try:
        return fn()
    except BaseException as exc:
        return exc


def test_same_key_waits_for_in_flight_request_then_replays(
    session_factory: Factory, engine: Engine, paused_poster: tuple[threading.Event, threading.Event]
) -> None:
    claimed, release = paused_poster
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())

    first, first_box = _in_background(
        lambda: transfer(session_factory, source, destination, 100, key=key)
    )
    assert claimed.wait(15)
    second, second_box = _in_background(
        lambda: transfer(session_factory, source, destination, 100, key=key)
    )
    wait_for_lock_waiters(engine, count=1)  # the second request is blocked on the key
    release.set()
    first.join(30)
    second.join(30)

    original, replay = first_box["result"], second_box["result"]
    assert not original.replayed and original.succeeded  # type: ignore[union-attr]
    assert replay.replayed  # type: ignore[union-attr]
    assert replay.response_body == original.response_body  # type: ignore[union-attr]
    assert balance(session_factory, source) == 900


def test_same_key_reports_in_progress_when_wait_exceeds_lock_timeout(
    session_factory: Factory, paused_poster: tuple[threading.Event, threading.Event]
) -> None:
    claimed, release = paused_poster
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())

    first, first_box = _in_background(
        lambda: transfer(session_factory, source, destination, 100, key=key)
    )
    assert claimed.wait(15)
    with pytest.raises(IdempotencyRequestInProgressError):
        transfer(session_factory, source, destination, 100, key=key, lock_timeout_ms=200)
    release.set()
    first.join(30)

    assert first_box["result"].succeeded  # type: ignore[union-attr]
    retry = transfer(session_factory, source, destination, 100, key=key)
    assert retry.replayed
    assert balance(session_factory, source) == 900


# --- HTTP level ------------------------------------------------------------------------


def test_completed_request_is_replayed_with_identical_body(client: TestClient) -> None:
    """Requirement 4."""
    a, b = api_create_account(client), api_create_account(client)
    api_fund_account(client, a["id"], 5_000)
    key = str(uuid.uuid4())

    first = post_transfer(client, a["id"], b["id"], 1_250, idempotency_key=key)
    replay = post_transfer(client, a["id"], b["id"], 1_250, idempotency_key=key)
    # Same request with upper-case UUIDs: canonical fingerprint -> still the same request.
    shouted = post_transfer(client, a["id"].upper(), b["id"].upper(), 1_250, idempotency_key=key)

    assert first.status_code == replay.status_code == shouted.status_code == 201
    assert "Idempotent-Replayed" not in first.headers
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.content == first.content  # byte-for-byte
    assert shouted.content == first.content
    assert get_balance(client, a["id"]) == 3_750


def test_key_reuse_with_different_body_returns_422(client: TestClient) -> None:
    a, b = api_create_account(client), api_create_account(client)
    api_fund_account(client, a["id"], 5_000)
    key = str(uuid.uuid4())
    assert post_transfer(client, a["id"], b["id"], 100, idempotency_key=key).status_code == 201

    response = post_transfer(client, a["id"], b["id"], 999, idempotency_key=key)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert get_balance(client, a["id"]) == 4_900


def test_business_rejection_is_the_keys_final_outcome(client: TestClient) -> None:
    a, b = api_create_account(client), api_create_account(client)
    key = str(uuid.uuid4())

    rejected = post_transfer(client, a["id"], b["id"], 500, idempotency_key=key)
    api_fund_account(client, a["id"], 1_000)  # funds arrive later...
    replay = post_transfer(client, a["id"], b["id"], 500, idempotency_key=key)

    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "INSUFFICIENT_FUNDS"
    # ...but the key already has an outcome: the same rejection is replayed.
    assert replay.status_code == 409
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.content == rejected.content
    assert get_balance(client, a["id"]) == 1_000
    # A new attempt needs a new key.
    assert post_transfer(client, a["id"], b["id"], 500).status_code == 201


def test_request_validation_errors_do_not_consume_the_key(client: TestClient) -> None:
    a, b = api_create_account(client), api_create_account(client)
    api_fund_account(client, a["id"], 1_000)
    key = str(uuid.uuid4())

    invalid = post_transfer(client, a["id"], b["id"], -5, idempotency_key=key)
    valid = post_transfer(client, a["id"], b["id"], 5, idempotency_key=key)

    assert invalid.status_code == 422
    assert valid.status_code == 201
    assert "Idempotent-Replayed" not in valid.headers


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": ""}, {"Idempotency-Key": "has space"}])
def test_missing_or_malformed_idempotency_key_returns_422(
    client: TestClient, session_factory: Factory, headers: dict[str, str]
) -> None:
    a, b = api_create_account(client), api_create_account(client)
    api_fund_account(client, a["id"], 1_000)

    response = client.post(
        "/api/v1/transfers",
        json={
            "source_account_id": a["id"],
            "destination_account_id": b["id"],
            "amount_minor": 100,
            "currency": "USD",
        },
        headers=headers,
    )

    assert response.status_code == 422
    locations = [d["loc"] for d in response.json()["error"]["details"]]
    assert ["header", "Idempotency-Key"] in locations
    assert merchant_transfer_count(session_factory) == 0
    assert idempotency_record(session_factory, "has space") is None
