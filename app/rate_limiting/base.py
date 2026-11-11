import math
import re
from dataclasses import dataclass
from typing import Protocol

# Bounds keep every intermediate value of the Redis script an exact integer well below
# 2^53 (Lua numbers are doubles): capacity * interval <= 3.6e11 "units".
MAX_CAPACITY = 100_000
MAX_REFILL_TOKENS = 100_000
MAX_REFILL_INTERVAL_MS = 3_600_000
_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """A token bucket: bursts of up to `capacity` requests, then a sustained rate of
    `refill_tokens` per `refill_interval_ms`, refilled continuously.

    `fail_open` decides what happens when the limiter itself is unavailable: True lets
    the request through (only acceptable for reads), False rejects it with 503.
    """

    name: str
    capacity: int
    refill_tokens: int
    refill_interval_ms: int
    fail_open: bool = False

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"policy name must match {_NAME.pattern}: {self.name!r}")
        for field, value, upper in (
            ("capacity", self.capacity, MAX_CAPACITY),
            ("refill_tokens", self.refill_tokens, MAX_REFILL_TOKENS),
            ("refill_interval_ms", self.refill_interval_ms, MAX_REFILL_INTERVAL_MS),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise ValueError(f"{field} must be an integer in [1, {upper}], got {value!r}")

    @property
    def full_refill_ms(self) -> int:
        """Time for an empty bucket to become full again."""
        return math.ceil(self.capacity * self.refill_interval_ms / self.refill_tokens)


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    policy: str
    limit: int  # the policy's burst capacity
    remaining: int | None  # whole requests still available right now, if known
    retry_after_seconds: int | None  # set when rejected: when one request will fit again


class RateLimiter(Protocol):
    def allow_request(self, scope: str, policy: RateLimitPolicy) -> RateLimitDecision:
        """Consume one request for `scope` under `policy`, or reject it.

        Raises RateLimiterUnavailableError if the decision cannot be made.
        """
        ...


def retry_after_seconds(retry_after_ms: int) -> int:
    # HTTP Retry-After is in whole seconds; round up so the client never retries early.
    return max(1, math.ceil(retry_after_ms / 1000))
