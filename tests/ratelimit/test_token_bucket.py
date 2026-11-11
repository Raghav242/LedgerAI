"""Token bucket semantics of the Lua script, driven by a controllable clock."""

import redis

from app.rate_limiting import RateLimitPolicy, RedisTokenBucketLimiter
from app.rate_limiting.redis_token_bucket import EXPIRY_MARGIN_MS
from tests.ratelimit.conftest import FakeClock

FIVE_PER_SECOND_BURST_10 = RateLimitPolicy(
    "write", capacity=10, refill_tokens=5, refill_interval_ms=1000
)


def _drain(limiter: RedisTokenBucketLimiter, policy: RateLimitPolicy, scope: str = "m") -> int:
    allowed = 0
    while limiter.allow_request(scope, policy).allowed:
        allowed += 1
    return allowed


def test_burst_capacity_then_empty_bucket(limiter: RedisTokenBucketLimiter) -> None:
    decisions = [limiter.allow_request("m", FIVE_PER_SECOND_BURST_10) for _ in range(12)]

    assert [d.allowed for d in decisions] == [True] * 10 + [False] * 2
    assert [d.remaining for d in decisions[:10]] == [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]
    empty = decisions[-1]
    assert empty.remaining == 0
    assert empty.retry_after_seconds == 1  # one token every 200 ms -> rounds up to 1 s
    assert empty.limit == 10 and empty.policy == "write"


def test_refill_is_continuous_and_exact(limiter: RedisTokenBucketLimiter, clock: FakeClock) -> None:
    _drain(limiter, FIVE_PER_SECOND_BURST_10)

    clock.advance(400)  # 5 tokens/s * 0.4 s = exactly 2 tokens
    assert _drain(limiter, FIVE_PER_SECOND_BURST_10) == 2
    clock.advance(199)  # 0.995 tokens: not yet a whole token
    assert not limiter.allow_request("m", FIVE_PER_SECOND_BURST_10).allowed
    clock.advance(1)  # 200 ms in total -> exactly one token
    assert limiter.allow_request("m", FIVE_PER_SECOND_BURST_10).allowed


def test_refill_never_exceeds_capacity(limiter: RedisTokenBucketLimiter, clock: FakeClock) -> None:
    _drain(limiter, FIVE_PER_SECOND_BURST_10)
    clock.advance(3_600_000)  # an hour idle would be 18000 tokens uncapped
    assert _drain(limiter, FIVE_PER_SECOND_BURST_10) == 10


def test_frequent_polling_does_not_lose_fractional_refill(
    limiter: RedisTokenBucketLimiter, clock: FakeClock
) -> None:
    """Float/rounding implementations that floor the refill on every call starve under
    1 ms polling. The integer-unit script accrues every millisecond exactly."""
    slow = RateLimitPolicy("slow", capacity=1, refill_tokens=1, refill_interval_ms=300)
    assert limiter.allow_request("m", slow).allowed
    for _ in range(299):
        clock.advance(1)
        assert not limiter.allow_request("m", slow).allowed
    clock.advance(1)
    assert limiter.allow_request("m", slow).allowed


def test_clock_going_backwards_never_refills(
    limiter: RedisTokenBucketLimiter, clock: FakeClock
) -> None:
    _drain(limiter, FIVE_PER_SECOND_BURST_10)
    clock.advance(-10_000)
    assert not limiter.allow_request("m", FIVE_PER_SECOND_BURST_10).allowed
    clock.advance(10_000 + 200)  # only real progress past the stored stamp counts
    assert limiter.allow_request("m", FIVE_PER_SECOND_BURST_10).allowed


def test_retry_after_matches_time_to_next_token(
    limiter: RedisTokenBucketLimiter, clock: FakeClock
) -> None:
    policy = RateLimitPolicy("p", capacity=1, refill_tokens=2, refill_interval_ms=5000)  # 2.5 s
    assert limiter.allow_request("m", policy).allowed
    assert limiter.allow_request("m", policy).retry_after_seconds == 3
    clock.advance(1500)
    assert limiter.allow_request("m", policy).retry_after_seconds == 1


def test_scopes_and_policies_have_separate_buckets(limiter: RedisTokenBucketLimiter) -> None:
    other_policy = RateLimitPolicy("read", capacity=3, refill_tokens=1, refill_interval_ms=1000)
    assert _drain(limiter, FIVE_PER_SECOND_BURST_10, scope="merchant-a") == 10
    assert _drain(limiter, FIVE_PER_SECOND_BURST_10, scope="merchant-b") == 10
    assert _drain(limiter, other_policy, scope="merchant-a") == 3


def test_buckets_expire_once_they_would_be_full(
    limiter: RedisTokenBucketLimiter, redis_client: redis.Redis, key_prefix: str
) -> None:
    for scope in ("a", "b", "c"):
        limiter.allow_request(scope, FIVE_PER_SECOND_BURST_10)

    keys = list(redis_client.scan_iter(f"{key_prefix}:*"))
    assert len(keys) == 3
    expected_ttl = FIVE_PER_SECOND_BURST_10.full_refill_ms + EXPIRY_MARGIN_MS  # 3000 ms
    for key in keys:
        ttl = redis_client.pttl(key)
        assert 0 < ttl <= expected_ttl  # every bucket expires: no unbounded key growth


def test_expired_bucket_behaves_as_full(
    limiter: RedisTokenBucketLimiter, redis_client: redis.Redis
) -> None:
    _drain(limiter, FIVE_PER_SECOND_BURST_10)
    redis_client.delete(limiter.key_for("m", FIVE_PER_SECOND_BURST_10))  # what expiry does
    assert _drain(limiter, FIVE_PER_SECOND_BURST_10) == 10


def test_keys_are_namespaced_and_include_the_policy_shape(
    limiter: RedisTokenBucketLimiter, key_prefix: str
) -> None:
    key = limiter.key_for("demo-merchant", FIVE_PER_SECOND_BURST_10)
    assert key == f"{key_prefix}:tb:write:10-5-1000:demo-merchant"
    changed = RateLimitPolicy("write", capacity=11, refill_tokens=5, refill_interval_ms=1000)
    assert limiter.key_for("demo-merchant", changed) != key
