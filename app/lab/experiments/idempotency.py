"""Idempotent retries: one transfer, many concurrent copies, one key (demo_idempotency.py)."""

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
from app.lab.schemas import IdempotencyParams, LabMetric
from app.lab.traffic import TRANSFERS_PATH, Observation, retry_delay, transfer_body

MAX_ATTEMPTS = 20
FUNDING_MULTIPLE = 10


async def run(ctx: ExperimentContext, params: IdempotencyParams) -> ExperimentOutcome:
    rec = ctx.recorder
    funding = params.amount_minor * FUNDING_MULTIPLE
    rec.phase("setup", "Creating two isolated lab accounts")
    source = await ctx.create_account("source")
    destination = await ctx.create_account("destination")
    await ctx.fund(source, funding)
    key = f"lab-{ctx.short_id}-order-1"
    body = transfer_body(source, destination, params.amount_minor)
    rec.note(f"Funded the source with {usd(funding)}", detail=f"Idempotency-Key: {key}")

    rec.phase(
        "duplicates",
        f"Sending {params.duplicates} concurrent copies with one Idempotency-Key",
        detail="Simulates an aggressive client or proxy retrying the same request.",
        total=params.duplicates,
    )
    attempts: list[Observation] = []
    async with ctx.http("burst", max_connections=min(params.duplicates, 100)) as http:
        gate = asyncio.Event()

        async def one(index: int) -> Observation:
            await gate.wait()
            obs: Observation | None = None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                obs = await http.send(
                    "POST",
                    TRANSFERS_PATH,
                    index=index,
                    attempt=attempt,
                    lane="duplicate",
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

        tasks = [asyncio.create_task(one(i)) for i in range(params.duplicates)]
        await asyncio.sleep(0)
        gate.set()
        finals = await asyncio.gather(*tasks)

        # Snapshot BEFORE the probes below move more money.
        recorded = await ctx.transfer_count(source)
        source_after = await ctx.balance(source)
        destination_after = await ctx.balance(destination)

        rec.phase("probes", "Probing key reuse and a fresh key", total=2)
        reuse = await http.send(
            "POST",
            TRANSFERS_PATH,
            index=params.duplicates,
            lane="key-reuse-probe",
            json=transfer_body(source, destination, params.amount_minor * 2),
            headers={"Idempotency-Key": key},
        )
        rec.request(reuse)
        source_after_reuse = await ctx.balance(source)
        fresh = await http.send(
            "POST",
            TRANSFERS_PATH,
            index=params.duplicates + 1,
            lane="fresh-key-probe",
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        rec.request(fresh)

    rec.phase("verify", "Counting committed transfers and reconciling")
    outcomes = Counter(o.outcome for o in finals)
    originals = outcomes["committed"]
    replays = outcomes["replayed"]
    in_progress_seen = sum(1 for o in attempts if o.outcome == "in_progress")
    transfer_ids = {
        o.json.get("id") for o in finals if o.status == 201 and isinstance(o.json, dict)
    }
    distinct_bodies = len({o.body for o in finals})
    fresh_id = fresh.json.get("id") if isinstance(fresh.json, dict) else None
    report = await ctx.reconcile()
    replica_counts = Counter(o.replica for o in attempts if o.replica)

    checks = [
        check(
            "all_201",
            "Every duplicate got 201",
            all(o.status == 201 for o in finals),
            f"final statuses: {dict(Counter(o.status for o in finals))}",
        ),
        check(
            "one_execution",
            "Exactly one request executed the transfer",
            originals == 1,
            f"{originals} original execution(s)",
        ),
        check(
            "all_others_replayed",
            "All other responses were replays",
            replays == params.duplicates - 1,
            f"{replays} response(s) with Idempotent-Replayed: true",
        ),
        check(
            "same_transfer",
            "All responses describe the same transfer",
            len(transfer_ids) == 1,
            f"{len(transfer_ids)} distinct transfer id(s)",
        ),
        check(
            "identical_bodies",
            "All response bodies are byte-identical",
            distinct_bodies == 1,
            f"{distinct_bodies} distinct body/bodies",
        ),
        check(
            "one_recorded",
            "Exactly one transfer recorded in PostgreSQL",
            recorded == 1,
            f"{recorded} transfer row(s) for the lab source account",
        ),
        check(
            "debited_once",
            "Source debited exactly once",
            source_after == funding - params.amount_minor,
            f"{usd(funding)} → {usd(source_after)}",
        ),
        check(
            "credited_once",
            "Destination credited exactly once",
            destination_after == params.amount_minor,
            f"destination {usd(destination_after)}",
        ),
        check(
            "reuse_rejected",
            "Same key with a different amount is rejected (422) and moves nothing",
            reuse.status == 422
            and reuse.code == "IDEMPOTENCY_KEY_REUSED"
            and source_after_reuse == source_after,
            f"{reuse.status} {reuse.code}; source still {usd(source_after_reuse)}",
        ),
        check(
            "new_key_new_transfer",
            "A new key is a new, separate transfer",
            fresh.status == 201 and fresh_id is not None and fresh_id not in transfer_ids,
            f"{fresh.status} {fresh.code or ''}".strip(),
        ),
        *ledger_checks(report, [source, destination]),
    ]

    latency = latency_summary(finals)
    summary = (
        f"{params.duplicates} concurrent copies of one {usd(params.amount_minor)} transfer "
        f"with one key: {originals} executed, {replays} replayed, {recorded} transfer "
        "recorded."
    )
    explanation = [
        "The first request to arrive INSERTs a claim row for (scope, Idempotency-Key) and "
        "executes the transfer in the same transaction as that claim.",
        "Copies that arrive while it is running block on the claim row (a unique index), "
        "then read the stored response once it commits and return it with "
        "Idempotent-Replayed: true, without touching balances.",
        "The stored request fingerprint makes the key mean exactly one request: the same "
        "key with a different amount is refused with 422 IDEMPOTENCY_KEY_REUSED.",
        f"Duplicates were served by {replica_label(dict(replica_counts))}: the claim is in "
        "PostgreSQL, so replicas cannot each execute their own copy.",
    ]
    if in_progress_seen:
        explanation.append(
            f"{in_progress_seen} attempt(s) were told 409 IDEMPOTENCY_REQUEST_IN_PROGRESS "
            "and retried with the same key."
        )

    return ExperimentOutcome(
        summary=summary,
        explanation=explanation,
        metrics=[
            LabMetric(key="duplicates", label="Duplicates submitted", value=params.duplicates),
            LabMetric(
                key="committed",
                label="Transfers committed",
                value=recorded,
                tone="success" if recorded == 1 else "destructive",
            ),
            LabMetric(key="replays", label="Replayed responses", value=replays, tone="info"),
            LabMetric(
                key="in_progress",
                label="Key in progress (409)",
                value=in_progress_seen,
                hint="retried with the same key",
            ),
            LabMetric(
                key="key_conflicts",
                label="Key reuse rejected",
                value=1 if reuse.code == "IDEMPOTENCY_KEY_REUSED" else 0,
                tone="warning",
                hint="422 probe",
            ),
            LabMetric(key="debited", label="Source debited", value=usd(funding - source_after)),
            LabMetric(key="p50", label="Median latency", value=latency["p50"], unit="ms"),
        ],
        checks=checks,
        outcome_counts=dict(outcomes),
        data={
            "idempotency_key": key,
            "transfer_ids": sorted(i for i in transfer_ids if i),
            "fresh_transfer_id": fresh_id,
            "source_account_id": source,
            "destination_account_id": destination,
            "funding_minor": funding,
            "balances_after_duplicates": {"source": source_after, "destination": destination_after},
            "reuse_probe": {"status": reuse.status, "code": reuse.code},
            "latency_ms": latency,
        },
    )
