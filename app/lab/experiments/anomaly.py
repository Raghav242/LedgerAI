"""AI Lab scenarios (Phase 5E): create activity through the normal transfer API, then
observe the AI worker's asynchronous verdict on the final ("probe") transfer.

Checks cover the SYSTEM: the transfer committed without waiting for the model, the event
reached the AI worker, exactly one result was stored. The model's verdict is reported, and
compared with the scenario's intent, but it is never a pass/fail check: a scenario being
"designed to be unusual" does not make a detection correct.
"""

import asyncio
import uuid

from sqlalchemy import func, select

from app.events.ids import event_id_for
from app.events.schemas import TRANSFER_COMPLETED
from app.lab.context import ExperimentContext, ExperimentOutcome, check, ledger_checks
from app.lab.schemas import AnomalyScenarioParams, LabMetric
from app.lab.traffic import TRANSFERS_PATH, Observation, transfer_body
from app.models import AnomalyResult, ProcessedEvent, Transfer

SCENARIOS = ("normal", "large_amount", "rapid_burst", "new_account")
INTENT_UNUSUAL = {"normal": False, "large_amount": True, "rapid_burst": True, "new_account": True}
HISTORY_MINOR = (2_800, 3_200, 2_500, 3_600, 3_000, 2_700)  # ~ $30, the account's habit
HISTORY_GAP_SECONDS = 4.0
PROBE_MINOR = {"normal": 3_100, "large_amount": 90_000, "rapid_burst": 3_000, "new_account": 60_000}
BURST_SIZE = 5
SCORE_TIMEOUT_SECONDS = 45
AI_CONSUMER = "anomaly-detector"

DESCRIPTION = {
    "normal": "six ~$30 transfers 4 s apart, then another ~$30 transfer",
    "large_amount": "six ~$30 transfers 4 s apart, then a $900 transfer (~30x the habit)",
    "rapid_burst": "six ~$30 transfers 4 s apart, then six more back to back",
    "new_account": "a brand-new account whose very first transfer is $600",
}


async def run(ctx: ExperimentContext, params: AnomalyScenarioParams) -> ExperimentOutcome:
    rec = ctx.recorder
    scenario = SCENARIOS[params.scenario]
    rec.phase("setup", f"Scenario: {scenario.replace('_', ' ')}", detail=DESCRIPTION[scenario])
    source = await ctx.create_account("source")
    destination = await ctx.create_account("destination")
    await ctx.fund(source, 200_000)

    observations: list[Observation] = []
    async with ctx.http("standard", max_connections=2) as http:

        async def send(amount: int, lane: str) -> Observation:
            obs = await http.send(
                "POST", TRANSFERS_PATH, index=len(observations), lane=lane,
                json=transfer_body(source, destination, amount),
                headers={"Idempotency-Key": f"lab-{ctx.short_id}-ai-{len(observations)}"},
            )  # fmt: skip
            rec.request(obs)
            observations.append(obs)
            return obs

        if scenario != "new_account":
            rec.phase("history", "Building the account's habit", total=len(HISTORY_MINOR))
            for amount in HISTORY_MINOR:
                await send(amount, "history")
                await asyncio.sleep(HISTORY_GAP_SECONDS)
        if scenario == "rapid_burst":
            rec.phase("burst", f"{BURST_SIZE} transfers back to back")
            for _ in range(BURST_SIZE):
                await send(3_000, "burst")
        rec.phase("probe", "Sending the transfer the model will score")
        probe = await send(PROBE_MINOR[scenario], "probe")

    if probe.outcome != "committed" or not isinstance(probe.json, dict):
        raise RuntimeError(f"the probe transfer was not committed: {probe.status} {probe.code}")
    transfer_id = uuid.UUID(probe.json["id"])
    event_id = event_id_for(TRANSFER_COMPLETED, transfer_id)
    result_when_answered = await _result(ctx, transfer_id)

    rec.phase("scoring", "Waiting for the AI worker (asynchronous, after COMMIT)")
    result = None
    for _ in range(SCORE_TIMEOUT_SECONDS * 4):
        result = await _result(ctx, transfer_id)
        if result is not None:
            break
        await asyncio.sleep(0.25)
    counts = await _counts(ctx, transfer_id, event_id)
    report = await ctx.reconcile()

    if result is not None:
        verdict = "flagged" if result["is_anomaly"] else "did not flag"
        rec.note(
            f"{result['model_version']} {verdict} the transfer: {_verdicts(result)}",
            level="warning" if result["is_anomaly"] else "success",
        )
    checks = [
        check(
            "committed",
            "The transfer committed (201)",
            probe.status == 201,
            f"{probe.status} in {probe.latency_ms:.0f} ms",
        ),
        check(
            "not_blocking",
            "The response did not wait for the model",
            result_when_answered is None,
            "no result existed yet when the 201 arrived"
            if result_when_answered is None
            else "a result already existed when the response arrived",
        ),
        check(
            "scored",
            "The AI worker scored the event",
            result is not None,
            f"scored {result['commit_to_scored_ms']:.0f} ms after the commit"
            if result
            else f"no result within {SCORE_TIMEOUT_SECONDS} s: is the ai-worker running?",
        ),
        check(
            "exactly_once",
            "Exactly one result and one receipt for the event",
            counts == (1, 1),
            f"results {counts[0]}, receipts {counts[1]}",
        ),
        *ledger_checks(report, [source, destination]),
    ]
    intent = INTENT_UNUSUAL[scenario]
    agrees = None if result is None else bool(result["is_anomaly"]) == intent
    return ExperimentOutcome(
        summary=_summary(scenario, result, intent),
        explanation=[
            "The transfer committed first; its transfer.completed event then went outbox -> "
            "publisher -> RabbitMQ -> AI worker, which computed features from the source "
            "account's EARLIER transfers only and scored them with an Isolation Forest.",
            "The score measures how easy the transfer is to isolate from the synthetic "
            "training data. It is not a probability of fraud.",
            "Agreement with the scenario's intent is not proof of correctness: the model was "
            "trained on synthetic data with a compressed clock.",
        ],
        metrics=[
            LabMetric(
                key="verdict",
                label="Model verdict",
                value="not scored"
                if result is None
                else ("anomaly" if result["is_anomaly"] else "not anomalous"),
                tone="warning" if result and result["is_anomaly"] else "neutral",
            ),
            LabMetric(
                key="score",
                label="Score (threshold)",
                value=f"{result['score']:.3f} ({result['threshold']:.3f})" if result else "—",
            ),
            LabMetric(
                key="latency",
                label="Commit -> scored",
                value=round(result["commit_to_scored_ms"]) if result else "—",
                unit="ms",
            ),
            LabMetric(
                key="response", label="Transfer response", value=round(probe.latency_ms), unit="ms"
            ),
        ],
        checks=checks,
        outcome_counts={"committed": sum(o.outcome == "committed" for o in observations)},
        data={
            "scenario": scenario,
            "description": DESCRIPTION[scenario],
            "intent_unusual": intent,
            "agrees_with_intent": agrees,
            "transfer_id": str(transfer_id),
            "event_id": str(event_id),
            "result": result,
        },
    )


def _verdicts(result: dict) -> str:
    """The model's and the rules' verdicts, each stated truthfully.

    With the hybrid decision (Phase 6B) the final verdict can come from the rules while
    the model's score is below its threshold, so the comparison sign is the MODEL's own.
    """
    model_flag = result["score"] >= result["threshold"]
    return (
        f"model score {result['score']:.4f} {'≥' if model_flag else '<'} threshold "
        f"{result['threshold']:.4f}; rule baseline "
        + ("fired" if result["rule_flags"] else "did not fire")
    )


def _summary(scenario: str, result: dict | None, intent: bool) -> str:
    if result is None:
        return (
            "The transfer committed, but no AI result arrived in time (is the ai-worker running?)."
        )
    flagged = result["is_anomaly"]
    head = (
        f"The detector {'flagged' if flagged else 'did not flag'} this transfer "
        f"({_verdicts(result)})."
    )
    if flagged == intent:
        tail = " That matches what the scenario was designed to look like."
    else:
        tail = (
            " The scenario was designed to look "
            + ("unusual" if intent else "normal")
            + ", so they disagree"
        )
        tail += (
            "; the rule baseline " + ("did" if result["rule_flags"] else "did not") + " flag it."
        )
    return head + tail


async def _result(ctx: ExperimentContext, transfer_id: uuid.UUID) -> dict | None:
    def read() -> dict | None:
        with ctx.session_factory() as session:
            row = session.scalar(
                select(AnomalyResult).where(AnomalyResult.transfer_id == transfer_id)
            )
            if row is None:
                return None
            transfer = session.get(Transfer, transfer_id)
            return {
                "model_version": row.model_version,
                "score": row.score,
                "threshold": row.threshold,
                "is_anomaly": row.is_anomaly,
                "features": row.features,
                "explanation": row.explanation,
                "rule_flags": row.rule_flags,
                "inference_ms": row.inference_ms,
                "delivery_attempt": row.delivery_attempt,
                "scored_at": row.scored_at.isoformat(),
                "commit_to_scored_ms": (row.scored_at - transfer.created_at).total_seconds() * 1000
                if transfer
                else None,
            }

    return await asyncio.to_thread(read)


async def _counts(
    ctx: ExperimentContext, transfer_id: uuid.UUID, event_id: uuid.UUID
) -> tuple[int, int]:
    def read() -> tuple[int, int]:
        with ctx.session_factory() as session:
            results = session.scalar(
                select(func.count())
                .select_from(AnomalyResult)
                .where(AnomalyResult.transfer_id == transfer_id)
            )
            receipts = session.scalar(
                select(func.count())
                .select_from(ProcessedEvent)
                .where(
                    ProcessedEvent.event_id == event_id, ProcessedEvent.consumer_name == AI_CONSUMER
                )
            )
            return results or 0, receipts or 0

    return await asyncio.to_thread(read)
