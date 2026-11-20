"""Load balancing: which replica served each request (round-robin part of
scripts/demo_load_balancing.py; stopping a replica stays a CLI-only demo)."""

import asyncio
import itertools
import math
from collections import Counter

from app.lab.context import ExperimentContext, ExperimentOutcome, check, latency_summary
from app.lab.schemas import LabMetric, LoadBalancingParams
from app.lab.traffic import Observation


async def run(ctx: ExperimentContext, params: LoadBalancingParams) -> ExperimentOutcome:
    rec = ctx.recorder
    rec.phase("setup", "Creating one isolated lab account to read")
    account = await ctx.create_account("reader")
    path = f"/api/v1/accounts/{account}"

    mode = "one after another" if params.concurrency == 1 else f"{params.concurrency} at a time"
    rec.phase(
        "requests",
        f"Sending {params.requests} reads through NGINX ({mode})",
        detail=f"GET {path}",
        total=params.requests,
    )
    observations: list[Observation | None] = [None] * params.requests
    async with ctx.http("burst", max_connections=params.concurrency) as http:
        semaphore = asyncio.Semaphore(params.concurrency)

        async def one(index: int) -> None:
            async with semaphore:
                obs = await http.send("GET", path, index=index, lane="read")
                observations[index] = obs
                rec.request(obs)

        await asyncio.gather(*(one(i) for i in range(params.requests)))

    rec.phase("verify", "Summarising the distribution")
    finals = [o for o in observations if o is not None]
    sequence = [o.replica or "unknown" for o in finals]
    counts = Counter(sequence)
    known = {name: n for name, n in counts.items() if name != "unknown"}
    replicas = len(known)
    ok_count = sum(1 for o in finals if o.status == 200)
    fair = params.requests / replicas if replicas else 0
    spread = (max(known.values()) - min(known.values())) if known else 0
    # NGINX round-robins per worker process; other clients' requests (the UI polling)
    # interleave too. Allow a small, explained tolerance instead of exact alternation.
    tolerance = max(2, math.ceil(0.25 * fair))
    alternations = sum(1 for a, b in itertools.pairwise(sequence) if a != b)
    latency = latency_summary(finals)
    latency_by_replica = {
        name: latency_summary(o for o in finals if (o.replica or "unknown") == name)
        for name in counts
    }

    checks = [
        check(
            "all_succeeded",
            "Every request succeeded",
            ok_count == params.requests,
            f"{ok_count}/{params.requests} answered 200",
        ),
        check(
            "replica_identified",
            "Every response named its replica (X-Served-By)",
            counts.get("unknown", 0) == 0,
            f"{counts.get('unknown', 0)} response(s) without X-Served-By"
            if counts.get("unknown")
            else "all identified",
            "Requires EXPOSE_REPLICA_HEADER=true (the Compose default).",
        ),
        check(
            "multiple_replicas",
            "Traffic reached more than one replica",
            replicas >= 2 if replicas else False,
            f"{replicas} replica(s): {', '.join(sorted(known)) or 'none'}",
        )
        if replicas != 1
        else check(
            "multiple_replicas",
            "Traffic reached more than one replica",
            None,
            "only one replica answered: the target is a single API process, not NGINX",
        ),
        check(
            "even_split",
            "Traffic is evenly split",
            spread <= tolerance if replicas >= 2 else None,
            f"spread {spread} request(s) between busiest and quietest replica "
            f"(tolerance {tolerance}, fair share {fair:.1f})"
            if replicas >= 2
            else "needs at least two replicas",
            "Round robin hands each new request to the next replica in turn.",
        ),
    ]

    summary = (
        f"{params.requests} reads were served by {replicas} replica(s): "
        + ", ".join(f"{name} {n}" for name, n in sorted(known.items()))
        + f" (spread {spread})."
    )
    explanation = [
        "NGINX is the only published entry point. For every request it picks the next "
        "upstream in round-robin order and proxies it, adding X-Request-ID; each replica "
        "stamps X-Served-By on its response.",
        "The replicas are stateless: balances, ledger and idempotency records live in "
        "PostgreSQL and quotas in Redis, so any replica can serve any request and the "
        "answers are identical.",
        f"Consecutive requests changed replica {alternations} time(s) out of "
        f"{max(0, len(sequence) - 1)}. NGINX keeps round-robin state per worker process, "
        "and other clients' requests (like this page's polling) interleave, so the pattern "
        "is close to, but not always exactly, 1-2-1-2.",
    ]

    return ExperimentOutcome(
        summary=summary,
        explanation=explanation,
        metrics=[
            LabMetric(key="total", label="Total requests", value=params.requests),
            LabMetric(key="replicas", label="Replicas seen", value=replicas, tone="info"),
            *[
                LabMetric(key=f"replica:{name}", label=name, value=n, unit="requests")
                for name, n in sorted(known.items())
            ],
            LabMetric(
                key="spread",
                label="Distribution spread",
                value=spread,
                tone="success" if replicas >= 2 and spread <= tolerance else "warning",
                hint=f"tolerance {tolerance}",
            ),
            LabMetric(key="p50", label="Median latency", value=latency["p50"], unit="ms"),
        ],
        checks=checks,
        outcome_counts=dict(Counter(o.outcome for o in finals)),
        data={
            "sequence": sequence,
            "counts": dict(counts),
            "fair_share": round(fair, 2),
            "spread": spread,
            "tolerance": tolerance,
            "alternations": alternations,
            "latency_ms": latency,
            "latency_by_replica_ms": latency_by_replica,
            "account_id": account,
            "path": path,
        },
    )
