"""Real Redis, real server time: atomicity, shared quota, failures and recovery.

Policies refill one token per hour, so refill during a test is impossible and the
expected counts are exact regardless of timing.
"""

import shutil
import subprocess
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from threading import Barrier

import pytest
import redis

from app.core.exceptions import RateLimiterUnavailableError
from app.rate_limiting import RateLimitPolicy, RedisTokenBucketLimiter
from tests.ratelimit.conftest import REDIS_TEST_URL, make_client
from tests.ratelimit.worker import consume


def no_refill(capacity: int) -> RateLimitPolicy:
    return RateLimitPolicy(
        "write", capacity=capacity, refill_tokens=1, refill_interval_ms=3_600_000
    )


def _concurrently(n: int, fn) -> list:  # type: ignore[no-untyped-def]
    barrier = Barrier(n)

    def run(i: int):  # type: ignore[no-untyped-def]
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def test_concurrent_consumption_is_atomic(redis_client: redis.Redis, key_prefix: str) -> None:
    limiter = RedisTokenBucketLimiter(redis_client, key_prefix=key_prefix)
    policy = no_refill(50)

    decisions = _concurrently(200, lambda _: limiter.allow_request("shared", policy))

    assert sum(d.allowed for d in decisions) == 50
    # Each admitted request saw a distinct remaining count: no two read the same state.
    assert sorted(d.remaining for d in decisions if d.allowed) == list(range(50))


def test_quota_is_shared_by_independent_clients(key_prefix: str, redis_client: redis.Redis) -> None:
    """Two limiters with separate connection pools, like two API replicas."""
    replica_a = RedisTokenBucketLimiter(make_client(), key_prefix=key_prefix)
    replica_b = RedisTokenBucketLimiter(make_client(), key_prefix=key_prefix)
    policy = no_refill(30)

    decisions = _concurrently(
        80, lambda i: (replica_a if i % 2 else replica_b).allow_request("shared", policy)
    )

    assert sum(d.allowed for d in decisions) == 30
    assert sum(not d.allowed for d in decisions) == 50


def test_quota_is_shared_across_processes(redis_client: redis.Redis, key_prefix: str) -> None:
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(consume, REDIS_TEST_URL, key_prefix, 40, 30) for _ in range(2)]
        allowed = [f.result(timeout=60) for f in futures]

    assert sum(allowed) == 30  # not 60, which is what two per-process limiters would allow


def test_script_cache_flush_is_recovered_transparently(
    redis_client: redis.Redis, key_prefix: str
) -> None:
    """A Redis restart empties the script cache; EVALSHA then fails with NOSCRIPT and the
    client must reload the script instead of failing requests."""
    limiter = RedisTokenBucketLimiter(redis_client, key_prefix=key_prefix)
    policy = no_refill(5)
    assert limiter.allow_request("m", policy).allowed
    redis_client.script_flush()
    assert limiter.allow_request("m", policy).remaining == 3


def test_dropped_connections_reconnect(key_prefix: str) -> None:
    client = make_client()
    limiter = RedisTokenBucketLimiter(client, key_prefix=key_prefix)
    policy = no_refill(5)
    assert limiter.allow_request("m", policy).allowed
    admin = make_client()
    admin.client_kill_filter(_type="normal", skipme=True)  # kill every other connection
    admin.close()
    # The pooled connection is dead; the next command reconnects (or fails fast once).
    for _ in range(2):
        try:
            decision = limiter.allow_request("m", policy)
            break
        except RateLimiterUnavailableError:
            continue
    assert decision.allowed and decision.remaining == 3
    client.close()


def test_unreachable_redis_fails_fast() -> None:
    limiter = RedisTokenBucketLimiter(make_client("redis://127.0.0.1:1/0", timeout_s=0.25))
    started = time.monotonic()
    with pytest.raises(RateLimiterUnavailableError):
        limiter.allow_request("m", no_refill(5))
    assert time.monotonic() - started < 2  # bounded by the connect timeout, no retry loop


@pytest.fixture
def disposable_redis() -> str:
    """A throwaway Redis container (NOT the stack's) that the test may stop and restart."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    name = f"ledgerflow-redis-restart-test-{uuid.uuid4().hex[:6]}"
    port = 6390
    # Same persistence settings as the stack's Redis (docker-compose.yml): no RDB, no AOF.
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:6379",
            "redis:7.4-alpine",
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "no",
        ],
        check=True,
        capture_output=True,
    )
    url = f"redis://127.0.0.1:{port}/0"
    _wait_for_redis(url)
    yield name, url
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _wait_for_redis(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if make_client(url).ping():
                return
        except redis.RedisError:
            time.sleep(0.2)  # polling interval while the container boots
    raise AssertionError(f"Redis at {url} did not come up")


def test_limiter_recovers_after_a_real_redis_restart(disposable_redis: tuple[str, str]) -> None:
    name, url = disposable_redis
    limiter = RedisTokenBucketLimiter(make_client(url), key_prefix="restart-test")
    policy = no_refill(3)
    assert limiter.allow_request("m", policy).allowed

    subprocess.run(["docker", "stop", "-t", "1", name], check=True, capture_output=True)
    started = time.monotonic()
    with pytest.raises(RateLimiterUnavailableError):
        limiter.allow_request("m", policy)
    assert time.monotonic() - started < 2  # outage detected quickly, not hung

    subprocess.run(["docker", "start", name], check=True, capture_output=True)
    _wait_for_redis(url)
    # No persistence: the restarted Redis is empty, so the bucket starts full again (a
    # documented, bounded over-admission of at most one burst). Scripts reload themselves.
    decision = limiter.allow_request("m", policy)
    assert decision.allowed and decision.remaining == 2
