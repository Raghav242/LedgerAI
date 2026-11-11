"""Rate limiting: a small Protocol with two interchangeable implementations.

- RedisTokenBucketLimiter: the real, distributed limiter shared by every API replica.
- InMemoryFixedWindowLimiter: per-process, for education/comparison and tests only.
"""

from app.rate_limiting.base import RateLimitDecision, RateLimiter, RateLimitPolicy
from app.rate_limiting.memory import InMemoryFixedWindowLimiter
from app.rate_limiting.redis_token_bucket import RedisTokenBucketLimiter, create_redis_client

__all__ = [
    "InMemoryFixedWindowLimiter",
    "RateLimitDecision",
    "RateLimitPolicy",
    "RateLimiter",
    "RedisTokenBucketLimiter",
    "create_redis_client",
]
