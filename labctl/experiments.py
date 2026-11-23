"""Phase 5D failure experiments, run by the local experiment controller only.

Each experiment: check preconditions (and abort WITHOUT disturbing anything if the stack is
not healthy), disturb exactly one allow-listed service, measure what clients and the event
pipeline observe, restore, verify recovery, and check the financial invariants.

They reuse the Experiment Lab machinery (isolated lab accounts, signed lab traffic through
NGINX, the recorder and timeline) and adapt the verified CLI demos in scripts/.
`guarded()` wraps every experiment so the journaled services are ALWAYS restored, also on
errors and timeouts, and a failed restoration turns the run into an explicit error.
"""

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.events.ids import event_id_for
from app.events.schemas import TRANSFER_COMPLETED
from app.lab.context import (
    ExperimentContext,
    ExperimentOutcome,
    check,
    ledger_checks,
    replica_label,
)
from app.lab.schemas import (
    LabMetric,
    RabbitMqOutageParams,
    RedisOutageParams,
    ReplicaFailureParams,
    WorkerCrashParams,
)
from app.lab.traffic import TRANSFERS_PATH, Observation, transfer_body
from labctl.infra import Infra, Journal

AUDIT_CONSUMER = "ledger-audit"
AUDIT_QUEUE = "ledgerflow.ledger-audit"
HOST_REDIS_URL = "redis://127.0.0.1:6379/0"


@dataclass
class Env:
    """What a failure experiment may use besides the lab context."""

    infra: Infra
    sessions: sessionmaker[Session]
    base_url: str

    # --- persisted state (read-only queries) ---------------------------------------------

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        def run() -> list[Any]:
            with self.sessions() as session:
                return list(session.execute(text(sql), params or {}).mappings())

        return await asyncio.to_thread(run)

    async def worker_report(self, component: str) -> dict[str, Any] | None:
        rows = await self.query(
            "SELECT status, details, beat_at, next_beat_before >= now() AS fresh "
            "FROM worker_heartbeats WHERE component = :c ORDER BY beat_at DESC LIMIT 1",
            {"c": component},
        )
        return dict(rows[0]) if rows else None

    async def outbox(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
        rows = await self.query(
            "SELECT id, status, attempts, last_error, published_at FROM outbox_events "
            "WHERE id = ANY(:ids)",
            {"ids": ids},
        )
        return {r["id"]: dict(r) for r in rows}

    async def effects(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
        rows = await self.query(
            "SELECT i.id, "
            "(SELECT count(*) FROM processed_events p WHERE p.event_id = i.id "
            " AND p.consumer_name = :c) AS receipts, "
            "(SELECT count(*) FROM event_audit_log a WHERE a.event_id = i.id) AS effects "
            "FROM unnest(CAST(:ids AS uuid[])) AS i(id)",
            {"ids": ids, "c": AUDIT_CONSUMER},
        )
        return {r["id"]: (r["receipts"], r["effects"]) for r in rows}

    # --- live probes from the host ---------------------------------------------------------

    async def redis_ok(self) -> bool:
        def ping() -> bool:
            client = redis.Redis.from_url(
                HOST_REDIS_URL, socket_timeout=1, socket_connect_timeout=1
            )
            try:
                return bool(client.ping())
            except redis.RedisError:
                return False
            finally:
                client.close()

        return await asyncio.to_thread(ping)

    async def ready_everywhere(self, probes: int = 4) -> bool:
        """Every probe through NGINX answers /health/ready 200 (round robin hits all)."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=3) as client:
            for _ in range(probes):
                try:
                    if (await client.get("/health/ready")).status_code != 200:
                        return False
                except httpx.HTTPError:
                    return False
        return True

    async def served_by(self, probes: int = 8) -> set[str]:
        seen = set()
        async with httpx.AsyncClient(base_url=self.base_url, timeout=3) as client:
            for _ in range(probes):
                try:
                    response = await client.get("/health/live")
                    if response.status_code == 200 and response.headers.get("x-served-by"):
                        seen.add(response.headers["x-served-by"])
                except httpx.HTTPError:
                    pass
        return seen


async def wait_until(
    condition: Callable[[], Awaitable[bool]], *, timeout: float, interval: float = 0.5
) -> float | None:
    """Seconds until `condition` held, or None if it never did within `timeout`."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if await condition():
            return round(time.monotonic() - started, 1)
        await asyncio.sleep(interval)
    return None


def _status(ok: bool | None) -> str:
    return "unknown" if ok is None else ("healthy" if ok else "unhealthy")


def _aborted(summary: str, checks: list, explanation: str) -> ExperimentOutcome:
    """Preconditions failed: nothing was disturbed."""
    return ExperimentOutcome(
        summary=summary,
        explanation=[explanation, "No service was stopped, because the stack was not healthy."],
        metrics=[],
        checks=checks,
        data={"aborted": True},
    )


# --- 1. Redis outage ----------------------------------------------------------------------


async def redis_outage(
    ctx: ExperimentContext, params: RedisOutageParams, env: Env
) -> ExperimentOutcome:
    rec = ctx.recorder
    rec.phase("before", "Confirming Redis is healthy")
    redis_before = await env.redis_ok()
    ready_before = await env.ready_everywhere()
    if not (redis_before and ready_before):
        return _aborted(
            "Precondition failed: Redis or a replica was not healthy",
            [
                check(
                    "redis_healthy_before",
                    "Redis healthy before the outage",
                    False,
                    f"redis ping {redis_before}, replicas ready {ready_before}",
                )
            ],
            "The experiment needs a healthy stack to show a clean failure and recovery.",
        )
    source = await ctx.create_account("source")
    destination = await ctx.create_account("destination")
    await ctx.fund(source, 10_000)
    keys = [f"lab-{ctx.short_id}-outage-{i}" for i in range(params.probe_writes)]

    async with ctx.http("standard", max_connections=4) as http:
        baseline = await http.send(
            "POST",
            TRANSFERS_PATH,
            index=0,
            lane="before",
            json=transfer_body(source, destination, 100),
            headers={"Idempotency-Key": f"lab-{ctx.short_id}-baseline"},
        )
        rec.request(baseline)
        count_before = await ctx.transfer_count(source)
        balances_before = (await ctx.balance(source), await ctx.balance(destination))

        rec.phase("outage", "Stopping Redis (docker compose stop redis)", total=params.probe_writes)
        await env.infra.stop("redis")
        stopped_at = time.monotonic()
        redis_during = await env.redis_ok()
        rec.note("Redis stopped", level="warning", detail="Writes must now fail closed.")
        probes: list[Observation] = []
        for i, key in enumerate(keys, start=1):
            obs = await http.send(
                "POST",
                TRANSFERS_PATH,
                index=i,
                lane="during",
                json=transfer_body(source, destination, 100),
                headers={"Idempotency-Key": key},
            )
            rec.request(obs)
            probes.append(obs)
        read = await http.send(
            "GET", f"/api/v1/accounts/{source}", index=len(keys) + 1, lane="during-read"
        )
        rec.request(read)
        count_during = await ctx.transfer_count(source)
        balances_during = (await ctx.balance(source), await ctx.balance(destination))
        remaining = params.outage_seconds - (time.monotonic() - stopped_at)
        if remaining > 0:
            await asyncio.sleep(remaining)

        rec.phase("recovery", "Starting Redis and waiting until every replica is ready")
        await env.infra.start("redis")
        started_at = time.monotonic()
        recovered_in = await wait_until(
            lambda: _all(env.redis_ok(), env.ready_everywhere()), timeout=60
        )
        redis_after = await env.redis_ok()
        rec.note(
            f"Redis back after {recovered_in} s"
            if recovered_in is not None
            else "Redis did NOT recover",
            level="success" if recovered_in is not None else "error",
        )
        retry = await http.send(
            "POST",
            TRANSFERS_PATH,
            index=len(keys) + 2,
            lane="after",
            json=transfer_body(source, destination, 100),
            headers={"Idempotency-Key": keys[0]},
        )
        rec.request(retry)
        replay = await http.send(
            "POST",
            TRANSFERS_PATH,
            index=len(keys) + 3,
            lane="after",
            json=transfer_body(source, destination, 100),
            headers={"Idempotency-Key": keys[0]},
        )
        rec.request(replay)
        count_after = await ctx.transfer_count(source)
        outage_total = round(started_at - stopped_at, 1)

    rec.phase("verify", "Checking balances, the ledger and reconciliation")
    report = await ctx.reconcile()
    latencies = [o.latency_ms for o in probes]
    fail_closed = all(o.status == 503 and o.code == "RATE_LIMITER_UNAVAILABLE" for o in probes)
    checks = [
        check(
            "redis_healthy_before",
            "Redis healthy and a baseline write committed",
            baseline.outcome == "committed",
            f"baseline {baseline.status}",
        ),
        check(
            "fail_closed",
            "Every write during the outage got 503 RATE_LIMITER_UNAVAILABLE",
            fail_closed,
            ", ".join(f"{o.status} {o.code}" for o in probes),
        ),
        check(
            "fail_fast",
            "Writes failed fast (no hang on the dead dependency)",
            max(latencies) < 2_000,
            f"slowest {max(latencies):.0f} ms",
        ),
        check(
            "no_financial_change",
            "No transfer, balance or ledger change during the outage",
            count_during == count_before and balances_during == balances_before,
            f"transfers {count_before}->{count_during}, "
            f"balances {balances_before}->{balances_during}",
        ),
        check(
            "reads_stay_up",
            "Reads kept working (fail open)",
            read.status == 200,
            f"GET account -> {read.status}",
        ),
        check(
            "recovered",
            "Redis and every replica recovered",
            recovered_in is not None,
            f"ready after {recovered_in} s" if recovered_in is not None else "not ready in 60 s",
        ),
        check(
            "retry_after_recovery",
            "Retrying a failed write with its key commits exactly once",
            retry.outcome == "committed"
            and replay.outcome == "replayed"
            and count_after == count_before + 1,
            f"retry {retry.status}, replay {replay.outcome}, "
            f"transfers {count_before}->{count_after}",
        ),
        *ledger_checks(report, [source, destination]),
    ]
    passed = all(c.status != "fail" for c in checks)
    return ExperimentOutcome(
        summary=(
            f"With Redis down, {len(probes)} of {len(probes)} writes were refused with 503 "
            f"in at most {max(latencies):.0f} ms and nothing changed; after recovery the retried "
            "key committed once."
            if passed
            else "The Redis outage did not behave as expected; see the failed checks."
        ),
        explanation=[
            "The write rate limit is a hard dependency: without Redis the API cannot know the "
            "global quota, so it refuses writes (fail closed) instead of risking overload.",
            "The refusal happens before any database session opens, which is why no balance, "
            "transfer or ledger entry changed.",
            "Reads fail open: the read quota protects capacity, not money.",
            "The client retried with the same Idempotency-Key after recovery, so the write "
            "happened exactly once.",
        ],
        metrics=[
            LabMetric(
                key="refused",
                label="Writes refused (503)",
                value=sum(o.status == 503 for o in probes),
                tone="warning",
            ),
            LabMetric(
                key="failure_latency",
                label="Slowest refusal",
                value=round(max(latencies)),
                unit="ms",
            ),
            LabMetric(
                key="recovery",
                label="Recovery after restart",
                value=recovered_in if recovered_in is not None else "failed",
                unit="s",
            ),
            LabMetric(key="reads", label="Read during outage", value=read.status or "no response"),
        ],
        checks=checks,
        outcome_counts=dict(Counter(o.outcome for o in [baseline, *probes, read, retry, replay])),
        data={
            "component": "redis",
            "component_status": [
                {"phase": "before", "status": _status(redis_before)},
                {"phase": "during", "status": _status(redis_during)},
                {"phase": "after", "status": _status(redis_after)},
            ],
            "outage_seconds": outage_total,
            "recovery_seconds": recovered_in,
        },
    )


async def _all(*checks: Awaitable[bool]) -> bool:
    return all(await asyncio.gather(*checks))


# --- 2. RabbitMQ outage ---------------------------------------------------------------------


async def rabbitmq_outage(
    ctx: ExperimentContext, params: RabbitMqOutageParams, env: Env
) -> ExperimentOutcome:
    rec = ctx.recorder
    rec.phase("before", "Confirming the publisher and consumer are connected to RabbitMQ")
    publisher = await env.worker_report("outbox-publisher")
    consumer = await env.worker_report("event-consumer")
    healthy = bool(
        publisher
        and publisher["fresh"]
        and publisher["details"].get("broker_connected")
        and consumer
        and consumer["fresh"]
        and consumer["details"].get("broker_connected")
    )
    if not healthy:
        return _aborted(
            "Precondition failed: the event workers were not connected to RabbitMQ",
            [
                check(
                    "broker_healthy_before",
                    "Publisher and consumer connected before the outage",
                    False,
                    "no fresh heartbeat reporting a broker connection",
                )
            ],
            "The experiment needs a working event pipeline to show what an outage changes.",
        )
    source = await ctx.create_account("source")
    destination = await ctx.create_account("destination")
    await ctx.fund(source, 100_000)

    rec.phase("outage", "Stopping RabbitMQ (docker compose stop rabbitmq)", total=params.transfers)
    await env.infra.stop("rabbitmq")
    stopped_at = time.monotonic()
    observations: list[Observation] = []
    event_ids: list[uuid.UUID] = []
    async with ctx.http("standard", max_connections=4) as http:
        for i in range(params.transfers):
            obs = await http.send(
                "POST",
                TRANSFERS_PATH,
                index=i,
                lane="during",
                json=transfer_body(source, destination, 250 + i),
                headers={"Idempotency-Key": f"lab-{ctx.short_id}-mq-{i}"},
            )
            rec.request(obs)
            observations.append(obs)
            if obs.outcome == "committed" and isinstance(obs.json, dict):
                event_ids.append(event_id_for(TRANSFER_COMPLETED, uuid.UUID(obs.json["id"])))
    remaining = params.outage_seconds - (time.monotonic() - stopped_at)
    if remaining > 0:
        await asyncio.sleep(remaining)
    during = await env.outbox(event_ids)
    publisher_during = await env.worker_report("outbox-publisher")
    rec.note(
        f"{sum(1 for r in during.values() if r['status'] != 'published')} of {len(event_ids)} "
        "events still in the outbox while the broker is down",
        level="warning",
    )

    rec.phase("recovery", "Starting RabbitMQ; waiting for publication and consumption")
    await env.infra.start("rabbitmq")
    restarted = time.monotonic()

    async def all_published() -> bool:
        rows = await env.outbox(event_ids)
        return len(rows) == len(event_ids) and all(
            r["status"] == "published" for r in rows.values()
        )

    async def all_consumed() -> bool:
        counts = await env.effects(event_ids)
        return all(receipts >= 1 for receipts, _ in counts.values())

    published_in = await wait_until(all_published, timeout=150, interval=1)
    consumed_in = (
        await wait_until(all_consumed, timeout=90, interval=1) if published_in is not None else None
    )
    # Both measured from the broker restart (consumed_in alone only covers the second wait).
    delivered_after = round(time.monotonic() - restarted, 1) if consumed_in is not None else None
    rec.note(
        f"Published {published_in} s and consumed {delivered_after} s after the restart"
        if consumed_in is not None
        else "Events were not all delivered in time",
        level="success" if consumed_in is not None else "error",
    )
    after = await env.outbox(event_ids)
    effects = await env.effects(event_ids)
    report = await ctx.reconcile()

    committed = [o for o in observations if o.outcome == "committed"]
    unpublished_during = [r for r in during.values() if r["status"] != "published"]
    exactly_once = all(v == (1, 1) for v in effects.values()) and len(effects) == len(event_ids)
    checks = [
        check(
            "broker_healthy_before",
            "Publisher and consumer connected before the outage",
            True,
            "fresh heartbeats with a broker connection",
        ),
        check(
            "transfers_commit",
            "Transfers committed while the broker was down",
            len(committed) == params.transfers,
            f"{len(committed)} of {params.transfers} got 201",
        ),
        check(
            "events_kept",
            "Their events stayed in the outbox (not lost, not published)",
            len(during) == len(event_ids) and len(unpublished_during) == len(event_ids),
            f"{len(unpublished_during)} of {len(event_ids)} unpublished during the outage",
        ),
        check(
            "published_after",
            "Every event was published after the broker returned",
            published_in is not None,
            f"after {published_in} s" if published_in is not None else "timed out",
        ),
        check(
            "consumed_once",
            "Each event was consumed exactly once (one receipt, one effect)",
            exactly_once,
            ", ".join(f"{r}/{e}" for r, e in effects.values()) or "no events",
        ),
        *ledger_checks(report, [source, destination]),
    ]
    passed = all(c.status != "fail" for c in checks)
    return ExperimentOutcome(
        summary=(
            f"All {len(committed)} transfers committed during the outage; their events waited in "
            f"the outbox and were delivered exactly once within {delivered_after} s of "
            "RabbitMQ returning."
            if passed
            else "The broker outage did not behave as expected; see the failed checks."
        ),
        explanation=[
            "A transfer and its event row commit in the same PostgreSQL transaction (the "
            "transactional outbox), so the API never needs the broker and never loses an event.",
            "Transfer success (the 201) and event delivery are separate: delivery happens later, "
            "asynchronously, and is at-least-once.",
            "The publisher connects to the broker before claiming rows, so while it is down the "
            "rows simply wait; afterwards it publishes with confirms.",
            "The consumer records a receipt per event id in the same transaction as its effect, "
            "so a duplicate delivery could never apply twice.",
        ],
        metrics=[
            LabMetric(
                key="committed",
                label="Transfers committed during outage",
                value=f"{len(committed)}/{params.transfers}",
                tone="success",
            ),
            LabMetric(
                key="waiting", label="Events waiting in outbox", value=len(unpublished_during)
            ),
            LabMetric(
                key="published_in",
                label="Published after restart",
                value=published_in if published_in is not None else "timed out",
                unit="s",
            ),
            LabMetric(
                key="consumed",
                label="Consumed exactly once",
                value=sum(v == (1, 1) for v in effects.values()),
            ),
        ],
        checks=checks,
        outcome_counts=dict(Counter(o.outcome for o in observations)),
        data={
            "component": "rabbitmq",
            "component_status": [
                {"phase": "before", "status": "healthy"},
                {"phase": "during", "status": "unhealthy"},
                {"phase": "after", "status": "healthy" if published_in is not None else "unknown"},
            ],
            "publisher_during": (publisher_during or {}).get("details", {}).get("summary"),
            "events": [
                {
                    "event_id": str(eid),
                    "during": during.get(eid, {}).get("status"),
                    "after": after.get(eid, {}).get("status"),
                    "publish_attempts": after.get(eid, {}).get("attempts"),
                    "receipts": effects.get(eid, (0, 0))[0],
                    "effects": effects.get(eid, (0, 0))[1],
                }
                for eid in event_ids
            ],
            "restart_to_consumed_seconds": delivered_after,
        },
    )


# --- 3. Worker crash ------------------------------------------------------------------------


async def worker_crash(
    ctx: ExperimentContext, params: WorkerCrashParams, env: Env
) -> ExperimentOutcome:
    rec = ctx.recorder
    rec.phase("before", "Confirming the consumer is running and the queue is empty")
    consumer = await env.worker_report("event-consumer")
    depth = await env.infra.queue_depth(AUDIT_QUEUE)
    if not (consumer and consumer["fresh"] and depth == (0, 0)):
        return _aborted(
            "Precondition failed: the consumer was not idle and healthy",
            [
                check(
                    "consumer_idle_before",
                    "Consumer healthy and its queue empty",
                    False,
                    f"heartbeat fresh={bool(consumer and consumer['fresh'])}, queue {depth}",
                )
            ],
            "Crash injection is keyed to one event; other traffic in flight would blur the result.",
        )
    source = await ctx.create_account("source")
    destination = await ctx.create_account("destination")
    await ctx.fund(source, params.amount_minor * 10)
    scenarios: list[dict[str, Any]] = []
    checks = [
        check("consumer_idle_before", "Consumer healthy and its queue empty", True, "queue (0, 0)")
    ]

    async with ctx.http("standard", max_connections=2) as http:
        for when in ("after", "before"):
            variable = f"CONSUMER_CRASH_{when.upper()}_COMMIT_EVENT"
            rec.phase(f"crash-{when}", f"Crash {when} the database commit")
            await env.infra.stop("event-consumer")
            obs = await http.send(
                "POST",
                TRANSFERS_PATH,
                index=len(scenarios),
                lane=f"crash-{when}",
                json=transfer_body(source, destination, params.amount_minor),
                headers={"Idempotency-Key": f"lab-{ctx.short_id}-crash-{when}"},
            )
            rec.request(obs)
            if obs.outcome != "committed" or not isinstance(obs.json, dict):
                raise RuntimeError(f"setup transfer failed: {obs.status} {obs.code}")
            event_id = event_id_for(TRANSFER_COMPLETED, uuid.UUID(obs.json["id"]))

            async def published(eid: uuid.UUID = event_id) -> bool:
                row = (await env.outbox([eid])).get(eid)
                return bool(row and row["status"] == "published")

            if await wait_until(published, timeout=60) is None:
                raise RuntimeError("the event was not published in 60 s")
            since = datetime.now(UTC).isoformat()
            await env.infra.recreate_consumer({variable: str(event_id)})
            rec.note(
                f"Consumer restarted with a fault hook for event {str(event_id)[:8]}…",
                detail=f"{variable} (development-only)",
            )
            duplicate_line = f"Duplicate delivery of event {event_id}"

            async def settled(
                eid: uuid.UUID = event_id, w: str = when, line: str = duplicate_line, t: str = since
            ) -> bool:
                if (await env.effects([eid])).get(eid) != (1, 1):
                    return False
                if await env.infra.queue_depth(AUDIT_QUEUE) != (0, 0):
                    return False
                return w != "after" or line in await env.infra.logs("event-consumer", since=t)

            took = await wait_until(settled, timeout=120, interval=1)
            logs = await env.infra.logs("event-consumer", since=since)
            restarts = await env.infra.restart_count("event-consumer")
            effects = (await env.effects([event_id])).get(event_id, (0, 0))
            depth_after = await env.infra.queue_depth(AUDIT_QUEUE)
            crashed = f"FAULT INJECTION: crashing {when} commit for event {event_id}" in logs
            duplicate = duplicate_line in logs
            scenarios.append(
                {
                    "crash_point": f"{when} commit",
                    "event_id": str(event_id),
                    "crashed": crashed,
                    "process_restarts": restarts,
                    "receipts": effects[0],
                    "effects": effects[1],
                    "duplicate_detected": duplicate,
                    "queue_after": list(depth_after or ()),
                    "settled_seconds": took,
                }
            )
            rec.note(
                f"Crash {when} commit: process died={crashed}, restarts={restarts}, "
                f"effects={effects[1]}, duplicate recognised={duplicate}",
                level="success" if took is not None else "error",
            )
            label = "after commit, before ack" if when == "after" else "before commit"
            checks += [
                check(
                    f"crashed_{when}",
                    f"The consumer process really died ({label})",
                    crashed and restarts >= 1,
                    f"log line {crashed}, restarts {restarts}",
                ),
                check(
                    f"once_{when}",
                    f"Exactly one effect despite the crash ({label})",
                    effects == (1, 1),
                    f"receipts {effects[0]}, effects {effects[1]}",
                ),
                check(
                    f"acked_{when}",
                    f"The message was finally acknowledged ({label})",
                    depth_after == (0, 0),
                    f"queue (ready, unacked) = {depth_after}",
                ),
                check(
                    f"redelivery_{when}",
                    "Redelivery recognised as a duplicate by its receipt"
                    if when == "after"
                    else "Rolled back, so redelivery processed it for real",
                    duplicate if when == "after" else not duplicate,
                    f"duplicate log line present: {duplicate}",
                ),
            ]
            await env.infra.recreate_consumer({})  # clear the fault hook

    report = await ctx.reconcile()
    checks += ledger_checks(report, [source, destination])
    passed = all(c.status != "fail" for c in checks)
    return ExperimentOutcome(
        summary=(
            "The consumer really crashed twice (after and before its commit); each event still "
            "had exactly one effect and was finally acknowledged."
            if passed
            else "A crash scenario did not behave as expected; see the failed checks."
        ),
        explanation=[
            "Deterministic fault injection: a development-only hook makes the consumer exit "
            "(os._exit) while handling one specific event id, on its first delivery only.",
            "After commit, before ack: the effect and its receipt are durable, the ack is lost. "
            "RabbitMQ redelivers; the receipt identifies the duplicate and it is acked once.",
            "Before commit: the transaction rolls back, nothing is durable, the redelivered "
            "message is processed for real.",
            "Either way: at-least-once delivery, exactly-once effect.",
        ],
        metrics=[
            LabMetric(
                key="crashes",
                label="Real process crashes",
                value=sum(1 for s in scenarios if s["crashed"]),
            ),
            LabMetric(
                key="effects",
                label="Effects per event",
                value=" / ".join(str(s["effects"]) for s in scenarios),
            ),
            LabMetric(
                key="duplicates",
                label="Duplicates recognised",
                value=sum(1 for s in scenarios if s["duplicate_detected"]),
            ),
            LabMetric(
                key="settled",
                label="Slowest recovery",
                value=max((s["settled_seconds"] or 0) for s in scenarios),
                unit="s",
            ),
        ],
        checks=checks,
        data={"component": "event-consumer", "scenarios": scenarios},
    )


# --- 4. API replica failure -------------------------------------------------------------------


async def replica_failure(
    ctx: ExperimentContext, params: ReplicaFailureParams, env: Env
) -> ExperimentOutcome:
    rec = ctx.recorder
    rec.phase("before", "Confirming both replicas serve traffic")
    if await env.served_by() != {"api-1", "api-2"}:
        return _aborted(
            "Precondition failed: NGINX was not routing to both api-1 and api-2",
            [
                check(
                    "both_before",
                    "Both replicas served before the failure",
                    False,
                    "start the default two-replica stack",
                )
            ],
            "The experiment stops api-2, so both replicas must be up first.",
        )
    source = await ctx.create_account("source")
    destination = await ctx.create_account("destination")
    await ctx.fund(source, 10_000)
    key = f"lab-{ctx.short_id}-failover"
    body = transfer_body(source, destination, 500)
    phases: dict[str, list[Observation]] = {"before": [], "during": [], "after": []}
    index = 0

    async with ctx.http("burst", max_connections=1) as http:

        async def reads(phase: str) -> None:
            nonlocal index
            for _ in range(params.requests_per_phase):
                obs = await http.send("GET", f"/api/v1/accounts/{source}", index=index, lane=phase)
                index += 1
                rec.request(obs)
                phases[phase].append(obs)

        await reads("before")
        rec.phase("during", "Stopping api-2 (docker compose stop api-2)")
        await env.infra.stop("api-2")
        await reads("during")
        first = await http.send(
            "POST",
            TRANSFERS_PATH,
            index=index,
            lane="during",
            json=body,
            headers={"Idempotency-Key": key},
        )
        rec.request(first)
        retry = await http.send(
            "POST",
            TRANSFERS_PATH,
            index=index + 1,
            lane="during",
            json=body,
            headers={"Idempotency-Key": key},
        )
        rec.request(retry)
        index += 2

        rec.phase("recovery", "Starting api-2; waiting until NGINX routes to it again")
        await env.infra.start("api-2")
        back_in = await wait_until(lambda: _served_both(env), timeout=90, interval=1)
        rec.note(
            f"api-2 serving again after {back_in} s"
            if back_in is not None
            else "api-2 did not come back",
            level="success" if back_in is not None else "error",
        )
        await reads("after")
        replay = await http.send(
            "POST",
            TRANSFERS_PATH,
            index=index,
            lane="after",
            json=body,
            headers={"Idempotency-Key": key},
        )
        rec.request(replay)

    transfers = await ctx.transfer_count(source)
    report = await ctx.reconcile()
    served = {p: Counter(o.replica for o in obs if o.status == 200) for p, obs in phases.items()}
    errors_during = [o for o in phases["during"] if o.status != 200]
    checks = [
        check(
            "both_before",
            "Before: reads served by both replicas",
            set(served["before"]) == {"api-1", "api-2"},
            replica_label(dict(served["before"])),
        ),
        check(
            "survivor_serves",
            "During: every successful read came from api-1",
            set(served["during"]) == {"api-1"},
            replica_label(dict(served["during"])),
        ),
        check(
            "no_lost_reads",
            "During: every read got an answer",
            not errors_during,
            f"{len(errors_during)} transient error(s): "
            + (", ".join(f"{o.status or o.code}" for o in errors_during) or "none"),
        ),
        check(
            "idempotent_failover",
            "A retried transfer executed once (replayed, same body)",
            first.outcome == "committed"
            and retry.outcome == "replayed"
            and replay.outcome == "replayed"
            and first.body == retry.body == replay.body
            and transfers == 1,
            f"{first.outcome}/{retry.outcome}/{replay.outcome}, {transfers} transfer(s)",
        ),
        check(
            "both_after",
            "After restart: both replicas serve again",
            set(served["after"]) == {"api-1", "api-2"} and back_in is not None,
            replica_label(dict(served["after"])),
        ),
        *ledger_checks(report, [source, destination]),
    ]
    passed = all(c.status != "fail" for c in checks)
    return ExperimentOutcome(
        summary=(
            f"With api-2 stopped, api-1 answered all {len(phases['during'])} reads; the retried "
            f"transfer executed once; api-2 was serving again {back_in} s after its restart."
            if passed
            else "The replica failure did not behave as expected; see the failed checks "
            "(transient errors are reported, not hidden)."
        ),
        explanation=[
            "Replicas keep no state, so any replica can answer any request, including retries.",
            "NGINX retries a failed connection on the next replica, and a stopped replica is "
            "skipped for a short fail_timeout, so clients see the survivor.",
            "The transfer's retry carried the same Idempotency-Key: the stored outcome in "
            "PostgreSQL was replayed instead of moving money twice.",
            "A restarted replica rejoins once NGINX re-resolves it (fixed addresses locally).",
        ],
        metrics=[
            LabMetric(
                key="during_ok",
                label="Reads answered while api-2 was down",
                value=f"{len(phases['during']) - len(errors_during)}/{len(phases['during'])}",
            ),
            LabMetric(
                key="transient",
                label="Transient errors",
                value=len(errors_during),
                tone="warning" if errors_during else "neutral",
            ),
            LabMetric(
                key="rejoin",
                label="api-2 rejoined after",
                value=back_in if back_in is not None else "failed",
                unit="s",
            ),
            LabMetric(key="transfers", label="Transfers for one key", value=transfers),
        ],
        checks=checks,
        outcome_counts=dict(Counter(o.outcome for obs in phases.values() for o in obs)),
        data={
            "component": "api-2",
            "sequences": {p: [o.replica or "error" for o in obs] for p, obs in phases.items()},
            "component_status": [
                {"phase": "before", "status": "healthy"},
                {"phase": "during", "status": "unhealthy"},
                {"phase": "after", "status": "healthy" if back_in is not None else "unknown"},
            ],
        },
    )


async def _served_both(env: Env) -> bool:
    return await env.served_by() == {"api-1", "api-2"}


# --- registry with guaranteed restoration --------------------------------------------------

FailureFn = Callable[[ExperimentContext, Any, Env], Awaitable[ExperimentOutcome]]

FAILURE_EXPERIMENTS: dict[str, FailureFn] = {
    "redis_outage": redis_outage,
    "rabbitmq_outage": rabbitmq_outage,
    "worker_crash": worker_crash,
    "replica_failure": replica_failure,
}


def guarded(
    fn: FailureFn,
    *,
    journal: Journal,
    sessions: sessionmaker[Session],
    base_url: str,
    runner=None,  # type: ignore[no-untyped-def]
) -> Callable[[ExperimentContext, Any], Awaitable[ExperimentOutcome]]:
    """Run `fn`, then ALWAYS restore what it disturbed (also on error, timeout, cancel)."""

    async def run(ctx: ExperimentContext, params: Any) -> ExperimentOutcome:
        infra = (
            Infra(str(ctx.run_id), journal, runner) if runner else Infra(str(ctx.run_id), journal)
        )
        env = Env(infra=infra, sessions=sessions, base_url=base_url)
        try:
            return await fn(ctx, params, env)
        finally:
            ctx.recorder.phase("restore", "Restoring every disturbed service")
            restored = await asyncio.shield(infra.restore())  # raises RecoveryFailedError
            ctx.recorder.note(
                f"Restored: {', '.join(restored)}" if restored else "Nothing left to restore",
                level="success",
            )

    return run
