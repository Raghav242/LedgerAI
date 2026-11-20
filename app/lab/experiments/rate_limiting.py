"""Distributed rate limiting: a burst against the real write policy (demo_rate_limiting.py).

The CLI demo redeploys the stack with a tiny quota. The lab must not control
infrastructure, so instead it uses the API's REAL write policy on a FRESH bucket that
belongs to this run alone (the signed lab scope). Same limiter, same Redis script, same
policy; only the bucket key differs, so the user's own browser quota is never touched.
"""

import asyncio
import math
import time
import uuid
from collections import Counter

from app.lab.context import (
    ExperimentContext,
    ExperimentOutcome,
    check,
    ledger_checks,
    replica_label,
    usd,
)
from app.lab.schemas import LabMetric, RateLimitingParams
from app.lab.traffic import TRANSFERS_PATH, LabHttp, Observation, transfer_body
from app.rate_limiting import InMemoryFixedWindowLimiter, RateLimitPolicy

AMOUNT_MINOR = 1  # one cent per admitted request
FUNDING_MINOR = 10_000


def per_replica_counterfactual(replicas: list[str], policy: RateLimitPolicy) -> int:
    """How many of these requests independent per-replica counters would have admitted.

    Each request is counted by the replica that served it, all inside one fixed window
    (the burst is far shorter than a window). This is what a per-process limiter does
    behind a load balancer: N replicas admit up to N x capacity.
    """
    clock_ms = 0
    limiters: dict[str, InMemoryFixedWindowLimiter] = {}
    admitted = 0
    for replica in replicas:
        limiter = limiters.setdefault(replica, InMemoryFixedWindowLimiter(lambda: clock_ms))
        admitted += limiter.allow_request("counterfactual", policy).allowed
    return admitted


def tokens_per_second(policy: RateLimitPolicy) -> float:
    return policy.refill_tokens * 1000 / policy.refill_interval_ms


async def _burst(
    ctx: ExperimentContext,
    http: LabHttp,
    source: str,
    destination: str,
    *,
    n: int,
    lane: str,
    first_index: int,
) -> tuple[list[Observation], float]:
    """n concurrent single-attempt transfers (a 429 is the observation, never retried)."""
    gate = asyncio.Event()
    body = transfer_body(source, destination, AMOUNT_MINOR)

    async def one(index: int) -> Observation:
        await gate.wait()
        obs = await http.send(
            "POST",
            TRANSFERS_PATH,
            index=index,
            lane=lane,
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        ctx.recorder.request(obs)
        return obs

    tasks = [asyncio.create_task(one(first_index + i)) for i in range(n)]
    await asyncio.sleep(0)
    started = time.monotonic()
    gate.set()
    observations = await asyncio.gather(*tasks)
    return list(observations), time.monotonic() - started


async def run(ctx: ExperimentContext, params: RateLimitingParams) -> ExperimentOutcome:
    rec = ctx.recorder
    policy = ctx.rate_limits.write
    rate = tokens_per_second(policy)
    is_redis = ctx.limiter_backend == "RedisTokenBucketLimiter"

    rec.phase("setup", "Creating two isolated lab accounts")
    source = await ctx.create_account("source")
    destination = await ctx.create_account("destination")
    await ctx.fund(source, FUNDING_MINOR)
    rec.note(
        f"Write policy under test: burst {policy.capacity}, refill {rate:g} tokens/s "
        f"({ctx.limiter_backend})",
        detail="A fresh bucket that belongs to this run only; your browser's quota is untouched.",
    )

    async with ctx.http("standard", max_connections=min(params.burst_requests, 100)) as http:
        rec.phase(
            "burst",
            f"Firing {params.burst_requests} concurrent $0.01 transfers",
            total=params.burst_requests,
        )
        burst, burst_elapsed = await _burst(
            ctx, http, source, destination, n=params.burst_requests, lane="burst", first_index=0
        )

        probe: list[Observation] = []
        probe_elapsed = 0.0
        if params.probe_requests:
            refill = min(policy.capacity, math.floor(params.refill_wait_seconds * rate))
            rec.phase(
                "refill",
                f"Waiting {params.refill_wait_seconds} s for the bucket to refill",
                detail=f"Expected ≈ {refill} new tokens.",
            )
            await asyncio.sleep(params.refill_wait_seconds)
            rec.phase(
                "probe",
                f"Firing {params.probe_requests} more transfers after the wait",
                total=params.probe_requests,
            )
            probe, probe_elapsed = await _burst(
                ctx,
                http,
                source,
                destination,
                n=params.probe_requests,
                lane="after-refill",
                first_index=params.burst_requests,
            )

    rec.phase("verify", "Checking the quota, Retry-After and money movement")
    statuses = Counter(o.status for o in burst)
    allowed = statuses[201]
    rejected = statuses[429]
    other = params.burst_requests - allowed - rejected
    refill_during_burst = math.floor(burst_elapsed * rate) + 1
    upper = policy.capacity + refill_during_burst
    lower = min(params.burst_requests, policy.capacity)
    rejections = [o for o in burst + probe if o.status == 429]
    retry_afters = sorted({o.retry_after_s for o in rejections if o.retry_after_s is not None})
    admitted_replicas = Counter(o.replica for o in burst if o.status == 201 and o.replica)
    served_replicas = [o.replica for o in burst if o.replica]
    multi_replica = len(set(served_replicas)) >= 2
    counterfactual = per_replica_counterfactual(served_replicas, policy) if multi_replica else None
    probe_allowed = sum(1 for o in probe if o.status == 201)
    total_admitted = allowed + probe_allowed
    destination_balance = await ctx.balance(destination)
    report = await ctx.reconcile()

    expected_refill = min(policy.capacity, math.floor(params.refill_wait_seconds * rate))
    probe_lower = min(params.probe_requests, expected_refill)
    probe_upper = policy.capacity + math.floor(probe_elapsed * rate) + 1

    checks = [
        check(
            "only_201_or_429",
            "Only 201 (admitted) or 429 (rejected) responses",
            other == 0 and all(o.status in (201, 429) for o in probe),
            f"burst statuses {dict(statuses)}",
        ),
        check(
            "global_quota",
            "Global quota respected during the burst",
            lower <= allowed <= upper,
            f"{allowed} admitted; allowed range {lower}-{upper} (burst {policy.capacity} + at "
            f"most {refill_during_burst} refilled during {burst_elapsed * 1000:.0f} ms)",
            "One bucket in Redis, decremented atomically by a Lua script, whichever replica asks.",
        ),
        check(
            "excess_rejected",
            "Requests beyond the quota were rejected with 429",
            (rejected == params.burst_requests - allowed)
            if params.burst_requests > upper
            else None,
            f"{rejected} rejected"
            if params.burst_requests > upper
            else f"burst of {params.burst_requests} fits in the quota; nothing to reject",
        ),
        check(
            "retry_after",
            "Every 429 carries a Retry-After header",
            all(o.retry_after_s is not None and o.retry_after_s >= 1 for o in rejections)
            if rejections
            else None,
            f"Retry-After values: {retry_afters or 'n/a'} s",
        ),
        check(
            "money_only_for_admitted",
            "Money moved only for admitted requests",
            destination_balance == total_admitted * AMOUNT_MINOR,
            f"destination {usd(destination_balance)} = {total_admitted} x $0.01",
            "A rejected request is refused before it opens a database session.",
        ),
        check(
            "shared_across_replicas",
            "Admitted requests were served by several replicas",
            len(admitted_replicas) >= 2 if multi_replica else None,
            replica_label(dict(admitted_replicas))
            if multi_replica
            else "only one replica answered: run the Compose stack to see this",
        ),
        check(
            "per_replica_would_overadmit",
            "Independent per-replica counters would have over-admitted",
            (counterfactual > allowed) if counterfactual is not None else None,
            f"{counterfactual} would pass vs {allowed} actually admitted"
            if counterfactual is not None
            else "needs at least two replicas",
        ),
        check(
            "refill",
            "Tokens refilled at the configured rate",
            (probe_lower <= probe_allowed <= probe_upper) if params.probe_requests else None,
            f"{probe_allowed} admitted after {params.refill_wait_seconds} s (expected "
            f"≈ {expected_refill}, allowed range {probe_lower}-{probe_upper})"
            if params.probe_requests
            else "probe skipped",
        ),
        check(
            "redis_backend",
            "Limiter is the shared Redis token bucket",
            True if is_redis else None,
            ctx.limiter_backend
            if is_redis
            else f"{ctx.limiter_backend}: per-process, not shared across replicas",
        ),
        *ledger_checks(report, [source, destination]),
    ]

    by_replica: dict[str, dict[str, int]] = {}
    for o in burst:
        slot = by_replica.setdefault(o.replica or "unknown", {"admitted": 0, "rejected": 0})
        slot["admitted" if o.status == 201 else "rejected"] += 1

    summary = (
        f"Burst of {params.burst_requests}: {allowed} admitted, {rejected} rejected with 429 "
        f"(quota {policy.capacity} + {rate:g}/s)."
        + (
            f" After {params.refill_wait_seconds} s, {probe_allowed} of "
            f"{params.probe_requests} were admitted again."
            if params.probe_requests
            else ""
        )
    )
    explanation = [
        f"The write policy is a token bucket: up to {policy.capacity} requests at once, "
        f"then {rate:g} per second. The bucket is ONE Redis hash per (policy, caller), "
        "updated by an atomic Lua script, so every replica draws from the same tokens.",
        f"{allowed} requests found a token; the other {rejected} were refused with 429 "
        "RATE_LIMITED before any database work, carrying Retry-After so clients know when "
        "to come back.",
    ]
    if counterfactual is not None:
        explanation.append(
            f"With independent counters in each replica, the same traffic would have let "
            f"{counterfactual} through: each replica would grant its own {policy.capacity}."
        )
    if params.probe_requests:
        explanation.append(
            f"After waiting {params.refill_wait_seconds} s the bucket had refilled by about "
            f"{expected_refill} tokens; {probe_allowed} of the next {params.probe_requests} "
            "requests were admitted."
        )

    return ExperimentOutcome(
        summary=summary,
        explanation=explanation,
        metrics=[
            LabMetric(key="burst", label="Burst size", value=params.burst_requests),
            LabMetric(key="allowed", label="Admitted (201)", value=allowed, tone="success"),
            LabMetric(key="rejected", label="Rejected (429)", value=rejected, tone="warning"),
            LabMetric(
                key="capacity",
                label="Bucket capacity",
                value=policy.capacity,
                hint=f"refill {rate:g}/s",
            ),
            LabMetric(
                key="retry_after",
                label="Retry-After",
                value=", ".join(str(v) for v in retry_afters) or "—",
                unit="s",
            ),
            LabMetric(
                key="probe_allowed",
                label="Admitted after refill",
                value=probe_allowed if params.probe_requests else "—",
                tone="info",
                hint=f"expected ≈ {expected_refill}" if params.probe_requests else None,
            ),
            LabMetric(
                key="counterfactual",
                label="Per-replica counters would admit",
                value=counterfactual if counterfactual is not None else "—",
                tone="destructive" if counterfactual and counterfactual > allowed else "neutral",
            ),
            LabMetric(
                key="burst_ms", label="Burst duration", value=round(burst_elapsed * 1000), unit="ms"
            ),
        ],
        checks=checks,
        outcome_counts=dict(Counter(o.outcome for o in burst + probe)),
        data={
            "policy": {
                "name": policy.name,
                "capacity": policy.capacity,
                "refill_tokens": policy.refill_tokens,
                "refill_interval_ms": policy.refill_interval_ms,
                "tokens_per_second": rate,
            },
            "limiter_backend": ctx.limiter_backend,
            "burst": {
                "requests": params.burst_requests,
                "allowed": allowed,
                "rejected": rejected,
                "elapsed_ms": round(burst_elapsed * 1000),
                "allowed_range": [lower, upper],
                "by_replica": by_replica,
                # Request index order: which got a token (true) and which a 429 (false).
                "admitted_sequence": [o.status == 201 for o in burst],
            },
            "probe": {
                "requests": params.probe_requests,
                "wait_seconds": params.refill_wait_seconds,
                "allowed": probe_allowed,
                "expected": expected_refill,
                "allowed_range": [probe_lower, probe_upper],
            },
            "retry_after_values": retry_afters,
            "counterfactual_per_replica": counterfactual,
            "source_account_id": source,
            "destination_account_id": destination,
        },
    )
