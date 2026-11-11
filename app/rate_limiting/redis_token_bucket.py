import logging
from collections.abc import Callable

import redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from app.core.config import Settings
from app.core.exceptions import RateLimiterUnavailableError
from app.rate_limiting.base import RateLimitDecision, RateLimitPolicy, retry_after_seconds

logger = logging.getLogger(__name__)

# Atomic token bucket. Redis runs a script to completion before serving any other
# command, so the read -> refill -> decide -> write sequence cannot interleave with
# another replica's request for the same bucket.
#
# Exact integer arithmetic (no floating point, no rounding drift): one millisecond of
# elapsed time adds exactly `rate` units and one request costs exactly `cost` units, where
#     rate = refill_tokens            cost = refill_interval_ms
#     capacity_units = capacity * refill_interval_ms
# so the sustained rate is refill_tokens per refill_interval_ms with zero precision loss.
# Policy bounds keep every value an exact integer far below 2^53.
TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])

local now
if ARGV[5] ~= '' then
  now = tonumber(ARGV[5])                       -- test clock
else
  local t = redis.call('TIME')                  -- one clock for every replica
  now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end

local tokens = capacity                         -- a missing (expired) bucket is full
local stamp = now
local state = redis.call('HMGET', key, 'tokens', 'ts')
if state[1] and state[2] then
  tokens = math.min(math.max(tonumber(state[1]), 0), capacity)
  stamp = tonumber(state[2])
  if now > stamp then                           -- never refill on a clock going backwards
    local deficit = capacity - tokens
    if deficit > 0 then
      local elapsed = now - stamp
      if elapsed >= math.ceil(deficit / rate) then
        tokens = capacity                       -- refills beyond capacity are discarded
      else
        tokens = tokens + elapsed * rate        -- elapsed * rate < deficit + rate: exact
      end
    end
    stamp = now
  end
end

local allowed = 0
local retry_after_ms = 0
if tokens >= cost then
  tokens = tokens - cost                        -- never below zero
  allowed = 1
else
  retry_after_ms = math.ceil((cost - tokens) / rate)
end

redis.call('HSET', key, 'tokens', string.format('%.0f', tokens), 'ts', string.format('%.0f', stamp))
redis.call('PEXPIRE', key, ttl_ms)
return {allowed, tokens, retry_after_ms}
"""

# After this long without requests a bucket is full again, so letting Redis delete it is
# indistinguishable from keeping it. This bounds key growth to active scopes.
EXPIRY_MARGIN_MS = 1000


def build_redis_client(
    url: str, *, connect_timeout_s: float, socket_timeout_s: float, max_connections: int
) -> redis.Redis:
    """Redis client with strictly bounded waiting at every step.

    BlockingConnectionPool: when all connections are busy, a caller waits up to
    `socket_timeout_s` for one instead of failing immediately (the default pool raises
    MaxConnectionsError at once, which under load would turn into spurious 503s).
    """
    pool = redis.BlockingConnectionPool.from_url(
        url,
        max_connections=max_connections,
        timeout=socket_timeout_s,
        socket_connect_timeout=connect_timeout_s,
        socket_timeout=socket_timeout_s,
        # No automatic retries: a retry could double-charge a bucket and would stretch a
        # Redis outage into a slow request. Fail fast; the API decides what to do.
        retry=Retry(NoBackoff(), 0),
        retry_on_timeout=False,
    )
    return redis.Redis(connection_pool=pool)


def create_redis_client(settings: Settings) -> redis.Redis:
    return build_redis_client(
        settings.redis_url,
        connect_timeout_s=settings.redis_connect_timeout_ms / 1000,
        socket_timeout_s=settings.redis_socket_timeout_ms / 1000,
        max_connections=settings.redis_max_connections,
    )


class RedisTokenBucketLimiter:
    """Distributed token bucket: one Redis hash per (policy, scope), shared by all replicas."""

    def __init__(
        self,
        client: redis.Redis,
        *,
        key_prefix: str = "ledgerflow:rl",
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._client = client
        self._key_prefix = key_prefix
        # Production uses Redis server TIME. Tests inject a controllable clock.
        self._clock_ms = clock_ms
        # EVALSHA with automatic re-load on NOSCRIPT (e.g. after a Redis restart).
        self._script = client.register_script(TOKEN_BUCKET_LUA)

    def key_for(self, scope: str, policy: RateLimitPolicy) -> str:
        # The policy's parameters are part of the key: changing a limit starts a fresh
        # bucket instead of reinterpreting state stored in different units.
        shape = f"{policy.capacity}-{policy.refill_tokens}-{policy.refill_interval_ms}"
        return f"{self._key_prefix}:tb:{policy.name}:{shape}:{scope}"

    def allow_request(self, scope: str, policy: RateLimitPolicy) -> RateLimitDecision:
        cost = policy.refill_interval_ms
        ttl_ms = policy.full_refill_ms + EXPIRY_MARGIN_MS
        now = "" if self._clock_ms is None else str(int(self._clock_ms()))
        try:
            allowed, tokens, retry_ms = self._script(
                keys=[self.key_for(scope, policy)],
                args=[policy.capacity * cost, policy.refill_tokens, cost, ttl_ms, now],
            )
        except RedisError as exc:
            logger.warning("Rate limiter unavailable policy=%s: %s", policy.name, exc)
            raise RateLimiterUnavailableError(
                "Rate limiting is temporarily unavailable", {"policy": policy.name}
            ) from exc
        return RateLimitDecision(
            allowed=bool(allowed),
            policy=policy.name,
            limit=policy.capacity,
            remaining=int(tokens) // cost,
            retry_after_seconds=None if allowed else retry_after_seconds(int(retry_ms)),
        )

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except RedisError:
            return False
