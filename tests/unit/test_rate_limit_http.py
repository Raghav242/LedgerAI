"""HTTP behaviour of rate limiting on paths that must never reach the database.

The app's session factory is replaced by one that fails the test if it is ever called:
a request rejected by the limiter (429) or by limiter unavailability (503) must not open
a database session, claim an Idempotency-Key or reach a service.
"""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.exceptions import RateLimiterUnavailableError
from app.main import create_app
from app.rate_limiting import RateLimitDecision, RateLimitPolicy

TRANSFER = {
    "source_account_id": str(uuid.uuid4()),
    "destination_account_id": str(uuid.uuid4()),
    "amount_minor": 100,
    "currency": "USD",
}


class RejectingLimiter:
    def allow_request(self, scope: str, policy: RateLimitPolicy) -> RateLimitDecision:
        return RateLimitDecision(False, policy.name, policy.capacity, 0, retry_after_seconds=7)


class UnavailableLimiter:
    def allow_request(self, scope: str, policy: RateLimitPolicy) -> RateLimitDecision:
        raise RateLimiterUnavailableError("Rate limiting is temporarily unavailable")


def _no_database():  # type: ignore[no-untyped-def]
    raise AssertionError("a rejected request must not open a database session")


def _client(limiter: object) -> Iterator[TestClient]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        postgres_user="u",
        postgres_password="p",
        rate_limit_backend="memory",
        enable_dev_endpoints=True,
        expose_replica_header=True,
        replica_id="unit-replica",
    )
    app = create_app(settings, rate_limiter=limiter)  # type: ignore[arg-type]
    app.state.session_factory = _no_database
    with TestClient(app) as client:
        yield client


@pytest.fixture
def rejecting_client() -> Iterator[TestClient]:
    yield from _client(RejectingLimiter())


@pytest.fixture
def unavailable_client() -> Iterator[TestClient]:
    yield from _client(UnavailableLimiter())


def test_quota_exceeded_returns_429_with_retry_after(rejecting_client: TestClient) -> None:
    response = rejecting_client.post(
        "/api/v1/transfers", json=TRANSFER, headers={"Idempotency-Key": "k-1"}
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.headers["Retry-After"] == "7"
    assert response.headers["X-RateLimit-Policy"] == "write"
    assert response.headers["X-RateLimit-Remaining"] == "0"
    assert response.headers["X-Served-By"] == "unit-replica"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/accounts"),
        ("post", f"/api/v1/accounts/{uuid.uuid4()}/fund"),
        ("get", "/api/v1/accounts"),
        ("get", "/api/v1/transfers"),
        ("get", "/api/v1/reconciliation"),
    ],
)
def test_every_business_route_is_rate_limited(
    rejecting_client: TestClient, method: str, path: str
) -> None:
    body = {} if method == "post" else None
    assert rejecting_client.request(method.upper(), path, json=body).status_code == 429


def test_health_endpoints_are_not_rate_limited(rejecting_client: TestClient) -> None:
    assert rejecting_client.get("/health/live").json() == {
        "status": "ok",
        "replica": "unit-replica",
    }


def test_financial_writes_fail_closed_when_limiter_is_unavailable(
    unavailable_client: TestClient,
) -> None:
    response = unavailable_client.post(
        "/api/v1/transfers", json=TRANSFER, headers={"Idempotency-Key": "k-2"}
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RATE_LIMITER_UNAVAILABLE"
    assert response.headers["Retry-After"] == "1"


def test_liveness_survives_limiter_outage(unavailable_client: TestClient) -> None:
    assert unavailable_client.get("/health/live").status_code == 200
