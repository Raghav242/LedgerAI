import threading
import time
from collections.abc import Callable

from app.rate_limiting.base import RateLimitDecision, RateLimitPolicy, retry_after_seconds


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class InMemoryFixedWindowLimiter:
    """Fixed-window counter held in this process's memory. For comparison and tests ONLY.

    The window length is the policy's full-refill time, so the long-run average rate
    matches the token bucket (`capacity` requests per window).

    Why it is not a real limit behind a load balancer: every replica keeps its own
    counter, so N replicas admit up to N * capacity per window. It also allows a burst of
    2 * capacity across a window boundary (end of one window + start of the next).
    """

    def __init__(self, clock_ms: Callable[[], int] = _monotonic_ms) -> None:
        self._clock_ms = clock_ms
        self._lock = threading.Lock()
        # One entry per (policy, scope): {key: (window_index, count)}. Old windows are
        # overwritten rather than accumulated, so memory is bounded by the number of scopes.
        self._windows: dict[tuple[str, str], tuple[int, int]] = {}

    def allow_request(self, scope: str, policy: RateLimitPolicy) -> RateLimitDecision:
        window_ms = policy.full_refill_ms
        now = self._clock_ms()
        window = now // window_ms
        key = (policy.name, scope)
        with self._lock:
            stored_window, count = self._windows.get(key, (window, 0))
            if stored_window != window:
                count = 0
            allowed = count < policy.capacity
            if allowed:
                count += 1
            self._windows[key] = (window, count)
        if allowed:
            return RateLimitDecision(
                allowed=True,
                policy=policy.name,
                limit=policy.capacity,
                remaining=policy.capacity - count,
                retry_after_seconds=None,
            )
        return RateLimitDecision(
            allowed=False,
            policy=policy.name,
            limit=policy.capacity,
            remaining=0,
            retry_after_seconds=retry_after_seconds((window + 1) * window_ms - now),
        )
