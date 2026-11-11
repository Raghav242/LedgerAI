"""Process-pool worker for the cross-process quota test (must be importable by spawn)."""

from app.rate_limiting import RateLimitPolicy, RedisTokenBucketLimiter


def consume(redis_url: str, key_prefix: str, attempts: int, capacity: int) -> int:
    from tests.ratelimit.conftest import make_client

    client = make_client(redis_url, timeout_s=2)
    limiter = RedisTokenBucketLimiter(client, key_prefix=key_prefix)
    policy = RateLimitPolicy(
        "write", capacity=capacity, refill_tokens=1, refill_interval_ms=3_600_000
    )
    try:
        return sum(limiter.allow_request("shared", policy).allowed for _ in range(attempts))
    finally:
        client.close()
