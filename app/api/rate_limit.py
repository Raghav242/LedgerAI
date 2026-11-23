"""Rate-limit enforcement for API routes.

Attached as route-level dependencies (`dependencies=[WriteRateLimit]`). FastAPI resolves
those before the endpoint's own parameters, so a rejected request never opens a database
session, never claims an Idempotency-Key and never reaches a service.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, Request

from app.api.dependencies import PrincipalDep
from app.core.config import Settings
from app.core.exceptions import RateLimiterUnavailableError, RateLimitExceededError
from app.core.principal import Principal
from app.observability.tracing import tracer
from app.rate_limiting import RateLimitDecision, RateLimiter, RateLimitPolicy

logger = logging.getLogger(__name__)

DECISION_STATE_KEY = "rate_limit_decision"


@dataclass(frozen=True, slots=True)
class RateLimits:
    limiter: RateLimiter
    read: RateLimitPolicy
    write: RateLimitPolicy
    # Experiment Lab traffic in "burst" mode (concurrency, idempotency, load-balancing
    # experiments) still goes through the real limiter, but under a large per-run quota:
    # those experiments are about locking and routing, not quotas. The rate-limiting
    # experiment uses the normal policies above on its own per-run bucket.
    lab_burst_read: RateLimitPolicy | None = None
    lab_burst_write: RateLimitPolicy | None = None

    def policy_for(self, principal: Principal, kind: Literal["read", "write"]) -> RateLimitPolicy:
        if principal.lab_rate_mode == "burst":
            lab = self.lab_burst_read if kind == "read" else self.lab_burst_write
            if lab is not None:
                return lab
        return self.read if kind == "read" else self.write


LAB_BURST_CAPACITY = 1_000


def build_lab_burst_policies() -> tuple[RateLimitPolicy, RateLimitPolicy]:
    """Large per-run buckets; same fail-open/fail-closed behaviour as the real policies."""
    read = RateLimitPolicy("lab-burst-read", LAB_BURST_CAPACITY, LAB_BURST_CAPACITY, 1000, True)
    write = RateLimitPolicy("lab-burst-write", LAB_BURST_CAPACITY, LAB_BURST_CAPACITY, 1000, False)
    return read, write


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
    with tracer.start_as_current_span("rate_limit.check") as span:
        span.set_attribute("ledgerflow.rate_limit.policy", policy.name)
        try:
            decision = limits.limiter.allow_request(scope, policy)
        except RateLimiterUnavailableError:
            span.set_attribute("ledgerflow.rate_limit.decision", "unavailable")
            span.set_attribute("ledgerflow.rate_limit.fail_open", policy.fail_open)
            if policy.fail_open:
                logger.warning("Rate limiter unavailable: FAIL-OPEN for policy=%s", policy.name)
                return
            raise
        span.set_attribute(
            "ledgerflow.rate_limit.decision", "allowed" if decision.allowed else "rejected"
        )
        if decision.remaining is not None:
            span.set_attribute("ledgerflow.rate_limit.remaining", decision.remaining)
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
    _enforce(request, principal.scope, request.app.state.rate_limits.policy_for(principal, "read"))


def enforce_write_limit(request: Request, principal: PrincipalDep) -> None:
    _enforce(request, principal.scope, request.app.state.rate_limits.policy_for(principal, "write"))


ReadRateLimit = Depends(enforce_read_limit)
WriteRateLimit = Depends(enforce_write_limit)
