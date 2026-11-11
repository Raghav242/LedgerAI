"""Real-Redis fixtures.

Tests use logical database 15 (the app uses 0) and a unique key prefix per test, and only
ever delete keys under that prefix, so they cannot disturb live rate-limit state.
"""

import os
import uuid
from collections.abc import Iterator

import pytest
import redis

from app.rate_limiting import RedisTokenBucketLimiter
from app.rate_limiting.redis_token_bucket import build_redis_client

REDIS_TEST_URL = os.environ.get("REDIS_TEST_URL", "redis://127.0.0.1:6379/15")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/ratelimit" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.redis)


def make_client(url: str = REDIS_TEST_URL, timeout_s: float = 0.25) -> redis.Redis:
    """Same client construction as the app (bounded, blocking pool, no retries)."""
    return build_redis_client(
        url, connect_timeout_s=timeout_s, socket_timeout_s=timeout_s, max_connections=50
    )


@pytest.fixture(scope="session")
def redis_client() -> Iterator[redis.Redis]:
    client = make_client(timeout_s=1)
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(
            f"Redis not reachable at {REDIS_TEST_URL} ({exc}); "
            "start it with: docker compose up -d --wait redis"
        )
    yield client
    client.close()


@pytest.fixture
def key_prefix(redis_client: redis.Redis) -> Iterator[str]:
    prefix = f"test:{uuid.uuid4().hex}"
    yield prefix
    keys = list(redis_client.scan_iter(f"{prefix}:*"))
    if keys:
        redis_client.delete(*keys)


class FakeClock:
    """Milliseconds; tests move time explicitly instead of sleeping."""

    def __init__(self, start_ms: int = 1_800_000_000_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(
    redis_client: redis.Redis, key_prefix: str, clock: FakeClock
) -> RedisTokenBucketLimiter:
    return RedisTokenBucketLimiter(redis_client, key_prefix=key_prefix, clock_ms=clock)
