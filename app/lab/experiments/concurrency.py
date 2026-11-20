"""Concurrent withdrawals: N transfers race for one balance (scripts/demo_concurrency.py)."""

import asyncio
import uuid
from collections import Counter

from app.lab.context import (
    ExperimentContext,
    ExperimentOutcome,
    check,
    latency_summary,
    ledger_checks,
    replica_label,
    usd,
)
from app.lab.schemas import ConcurrencyParams, LabMetric
from app.lab.traffic import TRANSFERS_PATH, Observation, retry_delay, transfer_body

MAX_ATTEMPTS = 20


async def run(ctx: ExperimentContext, params: ConcurrencyParams) -> ExperimentOutcome:
    rec = ctx.recorder
    rec.phase("setup", "Creating two isolated lab accounts")
    source = await ctx.create_account("source")
    destination = await ctx.create_account("destination")
    await ctx.fund(source, params.funding_minor)
    rec.note(
        f"Funded lab-{ctx.short_id}-source with {usd(params.funding_minor)} of simulated money",
        detail=f"source {source} · destination {destination}",
    )

    body = transfer_body(source, destination, params.amount_minor)
    attempts: list[Observation] = []
    rec.phase(
        "burst",
        f"Releasing {params.requests} concurrent {usd(params.amount_minor)} transfers",
        detail="All requests wait on one gate and are released together, through NGINX.",
        total=params.requests,
    )

    async with ctx.http("burst", max_connections=min(params.requests, 100)) as http:
        gate = asyncio.Event()

        async def one(index: int) -> Observation:
            key = str(uuid.uuid4())  # one logical transfer = one key, reused on retry
            await gate.wait()
            obs: Observation | None = None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                obs = await http.send(
                    "POST",
                    TRANSFERS_PATH,
                    index=index,
                    attempt=attempt,
                    lane="withdrawal",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                attempts.append(obs)
                final = not obs.retryable or attempt == MAX_ATTEMPTS
                rec.request(obs, final=final)
                if final:
                    break
                await asyncio.sleep(retry_delay(obs))
            assert obs is not None
            return obs

        tasks = [asyncio.create_task(one(i)) for i in range(params.requests)]
        await asyncio.sleep(0)  # every task reaches gate.wait() before the release
        gate.set()
        finals = await asyncio.gather(*tasks)

    rec.phase("verify", "Checking balances, ledger and reconciliation")
    outcomes = Counter(o.outcome for o in finals)
    succeeded = outcomes["committed"]
    insufficient = outcomes["insufficient_funds"]
    unexpected = params.requests - succeeded - insufficient
    retries = len(attempts) - len(finals)
    source_balance = await ctx.balance(source)
    destination_balance = await ctx.balance(destination)
    report = await ctx.reconcile()
    expected = min(params.requests, params.funding_minor // params.amount_minor)
    by_replica: dict[str, dict[str, int]] = {}
    for o in finals:
        slot = by_replica.setdefault(o.replica or "unknown", {})
        slot[o.outcome] = slot.get(o.outcome, 0) + 1
    replica_counts = Counter(o.replica for o in attempts if o.replica)

    checks = [
        check(
            "no_unexpected_errors",
            "No unexpected errors",
            unexpected == 0,
            f"{unexpected} request(s) ended in something other than success or insufficient funds",
        ),
        check(
            "exact_successes",
            f"Exactly {expected} transfers succeeded",
            succeeded == expected,
            f"{succeeded} succeeded (funding {usd(params.funding_minor)} ÷ "
            f"{usd(params.amount_minor)} = {expected})",
            "Row locks serialise the withdrawals, so each one sees the current balance.",
        ),
        check(
            "rest_rejected",
            "Every other request was rejected for insufficient funds",
            insufficient == params.requests - expected,
            f"{insufficient} rejected with 409 INSUFFICIENT_FUNDS",
        ),
        check(
            "no_overdraft",
            "Source balance never negative",
            source_balance >= 0,
            f"final source balance {usd(source_balance)}",
        ),
        check(
            "money_conserved",
            "Money conserved between the two accounts",
            source_balance + destination_balance == params.funding_minor,
            f"{usd(source_balance)} + {usd(destination_balance)} = "
            f"{usd(source_balance + destination_balance)}",
        ),
        check(
            "destination_exact",
            "Destination received exactly the successful transfers",
            destination_balance == succeeded * params.amount_minor,
            f"{usd(destination_balance)} = {succeeded} x {usd(params.amount_minor)}",
        ),
        *ledger_checks(report, [source, destination]),
    ]

    latency = latency_summary(finals)
    summary = (
        f"{params.requests} concurrent withdrawals of {usd(params.amount_minor)} from "
        f"{usd(params.funding_minor)}: {succeeded} succeeded, {insufficient} were rejected "
        f"for insufficient funds, and the source ended at {usd(source_balance)}."
    )
    explanation = [
        "Every transfer opens one transaction, claims its Idempotency-Key and then locks "
        "both account rows with SELECT … FOR UPDATE (in ascending id order, so two "
        "transfers can never deadlock).",
        f"While one transaction holds the source row, the others wait. Each sees the "
        f"balance left by the previous one, so only {expected} can fit: the rest find too "
        "little money and are rejected with 409 INSUFFICIENT_FUNDS. That rejection is "
        "stored under the key, so a retry would replay it.",
        f"Requests were served by {replica_label(dict(replica_counts))}. The lock lives in "
        "PostgreSQL, so it does not matter which replica handled a request.",
    ]
    if retries:
        explanation.append(
            f"{retries} attempt(s) got a retryable answer (lock timeout or same key in "
            "flight) and were retried with the SAME Idempotency-Key, which can never "
            "move money twice."
        )

    return ExperimentOutcome(
        summary=summary,
        explanation=explanation,
        metrics=[
            LabMetric(key="total", label="Total requests", value=params.requests),
            LabMetric(key="succeeded", label="Succeeded", value=succeeded, tone="success"),
            LabMetric(
                key="rejected",
                label="Insufficient funds",
                value=insufficient,
                tone="warning",
                hint="409, final answer",
            ),
            LabMetric(
                key="unexpected",
                label="Unexpected errors",
                value=unexpected,
                tone="destructive" if unexpected else "neutral",
            ),
            LabMetric(key="retries", label="Retried attempts", value=retries),
            LabMetric(
                key="source_balance",
                label="Final source balance",
                value=usd(source_balance),
                tone="destructive" if source_balance < 0 else "neutral",
            ),
            LabMetric(
                key="destination_balance",
                label="Final destination balance",
                value=usd(destination_balance),
            ),
            LabMetric(key="p50", label="Median latency", value=latency["p50"], unit="ms"),
        ],
        checks=checks,
        outcome_counts=dict(outcomes),
        data={
            "expected_successes": expected,
            "source_account_id": source,
            "destination_account_id": destination,
            "final_balances": {"source": source_balance, "destination": destination_balance},
            "by_replica": by_replica,
            "latency_ms": latency,
            "reconciliation_ok": report.ok,
        },
    )
