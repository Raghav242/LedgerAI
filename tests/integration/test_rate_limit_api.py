"""Rate limiting through the real app: PostgreSQL test database + real Redis limiter."""

import uuid
from collections.abc import Iterator

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.main import create_app
from app.rate_limiting import RedisTokenBucketLimiter
from app.rate_limiting.redis_token_bucket import build_redis_client
from app.services.transfer_service import TransferService
from tests.integration.conftest import post_transfer
from tests.integration.support import (
    balance,
    create_account,
    idempotency_record,
    ledger_entry_count,
    merchant_transfer_count,
)

pytestmark = pytest.mark.redis

REDIS_TEST_URL = "redis://127.0.0.1:6379/15"
WRITE_CAPACITY = 3


def _redis_or_skip() -> redis.Redis:
    client = build_redis_client(
        REDIS_TEST_URL, connect_timeout_s=1, socket_timeout_s=1, max_connections=10
    )
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis not reachable at {REDIS_TEST_URL}: {exc}")
    return client


@pytest.fixture
def limited_app(test_settings: Settings, engine: object) -> Iterator[tuple[TestClient, object]]:
    """App whose write policy allows 3 requests and (practically) never refills."""
    client = _redis_or_skip()
    prefix = f"test:{uuid.uuid4().hex}"
    settings = test_settings.model_copy(
        update={
            "rate_limit_write_capacity": WRITE_CAPACITY,
            "rate_limit_write_refill_tokens": 1,
            "rate_limit_write_refill_interval_ms": 3_600_000,
        }
    )
    app = create_app(settings, rate_limiter=RedisTokenBucketLimiter(client, key_prefix=prefix))
    with TestClient(app) as http:
        yield http, app
    keys = list(client.scan_iter(f"{prefix}:*"))
    if keys:
        client.delete(*keys)
    client.close()


def test_rejected_requests_never_reach_the_transfer_service(
    limited_app: tuple[TestClient, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, app = limited_app
    # Setup goes through the service layer, so it does not spend HTTP write quota.
    source = create_account(session_factory, funded=10_000)
    destination = create_account(session_factory)

    service_calls = {"n": 0}
    real_create = TransferService.create_transfer

    def counting_create(self, **kwargs):  # type: ignore[no-untyped-def]
        service_calls["n"] += 1
        return real_create(self, **kwargs)

    monkeypatch.setattr(TransferService, "create_transfer", counting_create)
    sessions_opened = {"n": 0}
    real_factory = app.state.session_factory  # type: ignore[attr-defined]

    def counting_factory():  # type: ignore[no-untyped-def]
        sessions_opened["n"] += 1
        return real_factory()

    app.state.session_factory = counting_factory  # type: ignore[attr-defined]

    keys = [str(uuid.uuid4()) for _ in range(5)]
    responses = [
        post_transfer(http, str(source), str(destination), 100, idempotency_key=k) for k in keys
    ]

    assert [r.status_code for r in responses] == [201, 201, 201, 429, 429]
    assert service_calls["n"] == WRITE_CAPACITY
    assert sessions_opened["n"] == WRITE_CAPACITY
    for key, response in zip(keys[3:], responses[3:], strict=True):
        assert response.json()["error"]["code"] == "RATE_LIMITED"
        assert int(response.headers["Retry-After"]) >= 1
        assert idempotency_record(session_factory, key) is None  # key never claimed
    assert merchant_transfer_count(session_factory) == 3
    assert ledger_entry_count(session_factory) == 2 + 3 * 2  # funding + 3 transfers
    assert balance(session_factory, source) == 9_700


def test_allowed_responses_carry_rate_limit_headers(
    limited_app: tuple[TestClient, object], session_factory: sessionmaker[Session]
) -> None:
    http, _ = limited_app
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)

    first = post_transfer(http, str(source), str(destination), 1)

    assert first.status_code == 201
    assert first.headers["X-RateLimit-Policy"] == "write"
    assert first.headers["X-RateLimit-Limit"] == str(WRITE_CAPACITY)
    assert first.headers["X-RateLimit-Remaining"] == str(WRITE_CAPACITY - 1)
    assert "Retry-After" not in first.headers
    # The body of an allowed request is the unchanged Phase 2 response.
    assert set(first.json()) >= {"id", "amount_minor", "status", "kind"}


def test_idempotent_replays_consume_quota(
    limited_app: tuple[TestClient, object], session_factory: sessionmaker[Session]
) -> None:
    """Policy: every HTTP request that is admitted costs one token, replay or not. A
    retry storm with one key is still a storm."""
    http, _ = limited_app
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())

    statuses = [
        post_transfer(http, str(source), str(destination), 10, idempotency_key=key).status_code
        for _ in range(4)
    ]

    assert statuses == [201, 201, 201, 429]  # original + 2 replays, then out of quota
    assert merchant_transfer_count(session_factory) == 1
    assert balance(session_factory, source) == 990


@pytest.fixture
def redis_down_client(test_settings: Settings, engine: object) -> Iterator[TestClient]:
    unreachable = build_redis_client(
        "redis://127.0.0.1:1/0", connect_timeout_s=0.2, socket_timeout_s=0.2, max_connections=5
    )
    settings = test_settings.model_copy(update={"rate_limit_backend": "redis"})
    app = create_app(settings, rate_limiter=RedisTokenBucketLimiter(unreachable))
    app.state.redis = unreachable  # readiness probes this client
    with TestClient(app) as http:
        yield http


def test_redis_outage_fails_writes_closed_and_reads_open(
    redis_down_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    source = create_account(session_factory, funded=1_000)
    destination = create_account(session_factory)
    key = str(uuid.uuid4())

    write = post_transfer(
        redis_down_client, str(source), str(destination), 100, idempotency_key=key
    )
    read = redis_down_client.get(f"/api/v1/accounts/{source}")

    # Financial mutation: refused, nothing touched, no silent per-replica fallback.
    assert write.status_code == 503
    assert write.json()["error"]["code"] == "RATE_LIMITER_UNAVAILABLE"
    assert idempotency_record(session_factory, key) is None
    assert merchant_transfer_count(session_factory) == 0
    assert balance(session_factory, source) == 1_000
    # Read: served (fail-open is an explicit per-policy choice for reads only).
    assert read.status_code == 200
    assert read.json()["balance_minor"] == 1_000
    assert "X-RateLimit-Policy" not in read.headers  # no decision was made


def test_health_endpoints_during_redis_outage(redis_down_client: TestClient) -> None:
    assert redis_down_client.get("/health/live").status_code == 200
    ready = redis_down_client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json()["redis"] == "unavailable"
    assert ready.json()["database"] == "ok"
