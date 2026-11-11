import pytest

from app.api.rate_limit import rate_limit_headers
from app.rate_limiting import InMemoryFixedWindowLimiter, RateLimitDecision, RateLimitPolicy
from app.rate_limiting.base import retry_after_seconds


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capacity": 0},
        {"capacity": -1},
        {"capacity": 100_001},
        {"capacity": 1.5},
        {"capacity": True},
        {"refill_tokens": 0},
        {"refill_interval_ms": 0},
        {"refill_interval_ms": 3_600_001},
        {"name": "Has Space"},
        {"name": ""},
    ],
)
def test_invalid_policies_are_rejected(kwargs: dict) -> None:
    values = {"name": "write", "capacity": 10, "refill_tokens": 1, "refill_interval_ms": 1000}
    values.update(kwargs)
    with pytest.raises(ValueError):
        RateLimitPolicy(**values)


def test_full_refill_time() -> None:
    policy = RateLimitPolicy("write", capacity=20, refill_tokens=10, refill_interval_ms=1000)
    assert policy.full_refill_ms == 2000
    assert (
        RateLimitPolicy("x", capacity=1, refill_tokens=3, refill_interval_ms=1000).full_refill_ms
        == 334
    )


@pytest.mark.parametrize(
    ("ms", "seconds"), [(0, 1), (1, 1), (999, 1), (1000, 1), (1001, 2), (2500, 3)]
)
def test_retry_after_rounds_up_to_whole_seconds(ms: int, seconds: int) -> None:
    assert retry_after_seconds(ms) == seconds


def test_headers_for_allowed_and_rejected_decisions() -> None:
    allowed = RateLimitDecision(True, "write", limit=20, remaining=7, retry_after_seconds=None)
    rejected = RateLimitDecision(False, "write", limit=20, remaining=0, retry_after_seconds=3)

    assert rate_limit_headers(allowed) == {
        "X-RateLimit-Policy": "write",
        "X-RateLimit-Limit": "20",
        "X-RateLimit-Remaining": "7",
    }
    assert rate_limit_headers(rejected)["Retry-After"] == "3"
    assert rate_limit_headers(rejected)["X-RateLimit-Remaining"] == "0"


class FakeClock:
    def __init__(self) -> None:
        self.now_ms = 1_000_000

    def __call__(self) -> int:
        return self.now_ms


POLICY = RateLimitPolicy("write", capacity=5, refill_tokens=5, refill_interval_ms=1000)  # 5/s


def test_fixed_window_allows_capacity_then_rejects_until_next_window() -> None:
    clock = FakeClock()
    clock.now_ms = 10_000  # start of a window (window = 1000 ms)
    limiter = InMemoryFixedWindowLimiter(clock)

    decisions = [limiter.allow_request("m", POLICY) for _ in range(7)]
    assert [d.allowed for d in decisions] == [True] * 5 + [False] * 2
    assert [d.remaining for d in decisions[:5]] == [4, 3, 2, 1, 0]
    assert decisions[-1].retry_after_seconds == 1

    clock.now_ms += 1000
    assert limiter.allow_request("m", POLICY).allowed


def test_fixed_window_boundary_burst_admits_twice_the_capacity() -> None:
    """The classic fixed-window weakness that the token bucket does not have."""
    clock = FakeClock()
    clock.now_ms = 10_999  # last millisecond of a window
    limiter = InMemoryFixedWindowLimiter(clock)
    first = sum(limiter.allow_request("m", POLICY).allowed for _ in range(5))
    clock.now_ms = 11_000  # one millisecond later: brand-new window
    second = sum(limiter.allow_request("m", POLICY).allowed for _ in range(5))
    assert first + second == 10  # 10 requests in 2 ms under a "5 per second" policy


def test_per_process_limiters_do_not_share_quota() -> None:
    """Two replicas with in-memory limiters admit 2x the configured limit."""
    clock = FakeClock()
    replica_a, replica_b = InMemoryFixedWindowLimiter(clock), InMemoryFixedWindowLimiter(clock)
    allowed = 0
    for i in range(20):  # round-robin across the two replicas
        limiter = replica_a if i % 2 == 0 else replica_b
        allowed += limiter.allow_request("m", POLICY).allowed
    assert allowed == 10  # instead of the intended 5


def test_scopes_and_policies_are_independent() -> None:
    limiter = InMemoryFixedWindowLimiter(FakeClock())
    read = RateLimitPolicy("read", capacity=1, refill_tokens=1, refill_interval_ms=1000)
    assert limiter.allow_request("a", POLICY).allowed
    assert limiter.allow_request("a", read).allowed
    assert limiter.allow_request("b", read).allowed
    assert not limiter.allow_request("a", read).allowed
