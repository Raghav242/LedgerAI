"""Rate-limit enforcement for API routes.

Attached as route-level dependencies (`dependencies=[WriteRateLimit]`). FastAPI resolves
those before the endpoint's own parameters, so a rejected request never opens a database
session, never claims an Idempotency-Key and never reaches a service.
"""

import logging
from dataclasses import dataclass

from fastapi import Depends, Request

from app.api.dependencies import PrincipalDep
from app.core.config import Settings
from app.core.exceptions import RateLimiterUnavailableError, RateLimitExceededError
from app.rate_limiting import RateLimitDecision, RateLimiter, RateLimitPolicy

logger = logging.getLogger(__name__)

DECISION_STATE_KEY = "rate_limit_decision"


@dataclass(frozen=True, slots=True)
class RateLimits:
    limiter: RateLimiter
    read: RateLimitPolicy
    write: RateLimitPolicy


def build_policies(settings: Settings) -> tuple[RateLimitPolicy, RateLimitPolicy]:
    read = RateLimitPolicy(
        name="read",
        capacity=settings.rate_limit_read_capacity,
        refill_tokens=settings.rate_limit_read_refill_tokens,
        refill_interval_ms=settings.rate_limit_read_refill_interval_ms,
        # Reads move no money: if Redis is down, serving them unthrottled (and logging it)
        # is better than taking the read API down with it.
        fail_open=True,
    )
    write = RateLimitPolicy(
        name="write",
        capacity=settings.rate_limit_write_capacity,
        refill_tokens=settings.rate_limit_write_refill_tokens,
        refill_interval_ms=settings.rate_limit_write_refill_interval_ms,
        # Financial mutations fail CLOSED: without a working shared limiter we do not
        # execute them (and never fall back to per-replica counters).
        fail_open=False,
    )
    return read, write


def rate_limit_headers(decision: RateLimitDecision) -> dict[str, str]:
    """De-facto X-RateLimit-* headers plus RFC 9110 Retry-After when rejected."""
    headers = {"X-RateLimit-Policy": decision.policy, "X-RateLimit-Limit": str(decision.limit)}
    if decision.remaining is not None:
        headers["X-RateLimit-Remaining"] = str(decision.remaining)
    if decision.retry_after_seconds is not None:
        headers["Retry-After"] = str(decision.retry_after_seconds)
    return headers


def _enforce(request: Request, scope: str, policy: RateLimitPolicy) -> None:
    limits: RateLimits = request.app.state.rate_limits
    try:
        decision = limits.limiter.allow_request(scope, policy)
    except RateLimiterUnavailableError:
        if policy.fail_open:
            logger.warning("Rate limiter unavailable: FAIL-OPEN for policy=%s", policy.name)
            return
        raise
    # Picked up by ResponseHeadersMiddleware for allowed and rejected responses alike.
    request.state.rate_limit_decision = decision
    if not decision.allowed:
        assert decision.retry_after_seconds is not None
        raise RateLimitExceededError(
            "Rate limit exceeded; retry later",
            retry_after_seconds=decision.retry_after_seconds,
            policy=decision.policy,
        )


def enforce_read_limit(request: Request, principal: PrincipalDep) -> None:
    _enforce(request, principal.scope, request.app.state.rate_limits.read)


def enforce_write_limit(request: Request, principal: PrincipalDep) -> None:
    _enforce(request, principal.scope, request.app.state.rate_limits.write)


ReadRateLimit = Depends(enforce_read_limit)
WriteRateLimit = Depends(enforce_write_limit)
