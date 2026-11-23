"""The allow-list: the only experiments the lab can run, with human-readable descriptions.

The parameter specs shown in the UI are derived from the same Pydantic models that
validate a run request, so the bounds the UI explains are exactly the bounds enforced.
"""

from typing import Any

from app.lab.schemas import PARAMS_BY_EXPERIMENT, ExperimentDefinition, ParamSpec

RISK_NOTE = (
    "Local development only. Creates its own uniquely named lab accounts funded with "
    "simulated money and never touches other accounts. Bounded request counts, one run at "
    "a time, no infrastructure control."
)


def param_specs(experiment: str) -> list[ParamSpec]:
    schema: dict[str, Any] = PARAMS_BY_EXPERIMENT[experiment].model_json_schema()
    return [
        ParamSpec(
            name=name,
            label=prop["title"],
            description=prop["description"],
            unit=prop["unit"],
            default=prop["default"],
            minimum=prop["minimum"],
            maximum=prop["maximum"],
        )
        for name, prop in schema["properties"].items()
    ]


_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "concurrency",
        "title": "Concurrent withdrawals",
        "summary": "Many transfers race to spend the same balance at the same instant.",
        "concept": "Row locking (SELECT … FOR UPDATE) and atomic transactions",
        "expected": [
            "Exactly funding ÷ amount transfers succeed (10 with the defaults).",
            "Every other request is rejected with 409 INSUFFICIENT_FUNDS.",
            "The source balance never goes negative; money is conserved.",
            "Ledger and reconciliation invariants still hold.",
        ],
        "how_it_works": [
            "Creates two isolated lab accounts and funds the source.",
            "Releases all transfers at once through NGINX to every replica.",
            "Retries only retryable answers, always with the same Idempotency-Key.",
            "Checks balances, the ledger and global reconciliation.",
        ],
        "estimated_seconds": 5,
    },
    {
        "id": "idempotency",
        "title": "Idempotent retries",
        "summary": "One transfer sent many times concurrently with the same Idempotency-Key.",
        "concept": "Idempotency keys: claim-and-complete in one transaction, replay on retry",
        "expected": [
            "Exactly one request executes the transfer; all others are replays.",
            "Every response describes the same transfer, byte for byte.",
            "Money moves once. Reusing the key with a different amount is rejected (422).",
            "A new key is a new, separate transfer.",
        ],
        "how_it_works": [
            "Creates two isolated lab accounts and funds the source.",
            "Fires every duplicate at once with one key, through NGINX.",
            "Probes key reuse with a different body, then a fresh key.",
            "Counts committed transfers in the database and reconciles the ledger.",
        ],
        "estimated_seconds": 4,
    },
    {
        "id": "rate_limiting",
        "title": "Distributed rate limiting",
        "summary": "A burst of writes against the real write quota, shared by every replica.",
        "concept": "A global token bucket in Redis, enforced atomically across replicas",
        "expected": [
            "About `capacity` requests are admitted (plus tokens refilled during the burst).",
            "The rest get 429 RATE_LIMITED with a Retry-After header and move no money.",
            "Admitted requests are spread over replicas, yet the quota is global.",
            "After the wait, roughly wait x refill-rate tokens are available again.",
        ],
        "how_it_works": [
            "Uses the API's real write policy on a fresh bucket that belongs to this run "
            "only, so your own browser quota is untouched.",
            "Fires the burst through NGINX, waits, then fires a second burst.",
            "Compares with what independent per-replica counters would have admitted.",
        ],
        "estimated_seconds": 6,
    },
    {
        "id": "load_balancing",
        "title": "Load balancing",
        "summary": "Read requests through NGINX, recording which replica served each one.",
        "concept": "Round-robin load balancing over stateless replicas",
        "expected": [
            "Requests are spread evenly across the running replicas.",
            "Every request succeeds, whichever replica serves it.",
            "The sequence shows the round-robin pattern (1, 2, 1, 2, …).",
        ],
        "how_it_works": [
            "Creates one isolated lab account to read.",
            "Sends the requests through NGINX and records X-Served-By per response.",
            "Summarises the distribution and its spread. No replica is stopped.",
        ],
        "estimated_seconds": 3,
    },
]


INFRA_RISK_NOTE = (
    "Stops and restarts ONE allow-listed service of the local Compose stack, then verifies "
    "recovery. Runs only through the local experiment controller (python -m labctl "
    "--enable-failures), never through the API. One run at a time, bounded duration, "
    "automatic restoration."
)
CONTROLLER_ONLY = (
    "Infrastructure experiments run only through the local experiment controller: "
    "python -m labctl --enable-failures"
)

_FAILURE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "redis_outage",
        "title": "Redis outage",
        "summary": "Stop Redis: financial writes fail closed, nothing changes, then recover.",
        "concept": "Fail-closed writes, fail-open reads, and clean recovery",
        "expected": [
            "While Redis is down, every transfer gets 503 RATE_LIMITER_UNAVAILABLE, fast.",
            "No balance, transfer or ledger entry changes during the outage.",
            "Reads keep working (they fail open).",
            "After Redis restarts, retrying the SAME Idempotency-Key commits exactly once.",
        ],
        "how_it_works": [
            "Confirms Redis is healthy and a baseline transfer commits.",
            "docker compose stop redis (allow-listed), then sends the probe writes.",
            "docker compose start redis, waits until every replica is ready again.",
            "Retries one failed probe with its original key and reconciles the ledger.",
        ],
        "estimated_seconds": 15,
    },
    {
        "id": "rabbitmq_outage",
        "title": "RabbitMQ outage",
        "summary": "Stop the broker: transfers still commit and their events wait in the outbox.",
        "concept": "Transactional outbox: no dual write, at-least-once delivery after recovery",
        "expected": [
            "Transfers commit (201) while the broker is down: the API never talks to it.",
            "Their events stay unpublished in the outbox; publish attempts fail and are retried.",
            "After the broker restarts, every event is published and consumed exactly once.",
            "Transfer success and event delivery are reported separately.",
        ],
        "how_it_works": [
            "Confirms the publisher reports a healthy broker connection.",
            "docker compose stop rabbitmq (allow-listed), then commits the transfers.",
            "Watches the outbox rows, then docker compose start rabbitmq.",
            "Waits for publication and consumer receipts; counts effects per event.",
        ],
        "estimated_seconds": 40,
    },
    {
        "id": "worker_crash",
        "title": "Worker crash and redelivery",
        "summary": "Crash the consumer before and after its commit; each effect happens once.",
        "concept": "Manual acks, consumer receipts, at-least-once delivery",
        "expected": [
            "Crash after commit, before ack: the message is redelivered, recognised as a "
            "duplicate by its receipt and acknowledged; one effect.",
            "Crash before commit: the transaction rolls back, the message is redelivered and "
            "processed for real; one effect.",
            "The queue drains: RabbitMQ eventually gets the ack.",
        ],
        "how_it_works": [
            "Stops the consumer, commits a transfer and waits until its event is queued.",
            "Restarts the consumer with a development-only fault hook keyed to that event id.",
            "The process really exits (os._exit) at the chosen point; Docker restarts it.",
            "Counts receipts and audit rows, reads the queue depth, restores the consumer.",
        ],
        "estimated_seconds": 45,
    },
    {
        "id": "replica_failure",
        "title": "API replica failure",
        "summary": "Stop api-2: NGINX routes to api-1, idempotency holds, then api-2 comes back.",
        "concept": "Stateless replicas behind a load balancer; recovery after restart",
        "expected": [
            "Before: reads are spread over api-1 and api-2.",
            "During: requests are served by api-1; any transient 502/504 is counted, not hidden.",
            "A retried transfer with the same key replays instead of executing twice.",
            "After the restart both replicas serve again and the ledger reconciles.",
        ],
        "how_it_works": [
            "Sends sequential reads through NGINX and records X-Served-By.",
            "docker compose stop api-2 (allow-listed), repeats the reads, sends a transfer.",
            "docker compose start api-2 and waits until NGINX routes to it again.",
            "Replays the transfer's key on the restored stack and reconciles.",
        ],
        "estimated_seconds": 30,
    },
]


_AI_DEFINITION: dict[str, Any] = {
    "id": "anomaly_scenario",
    "title": "AI anomaly scenario",
    "summary": "Create activity through the transfer API and read the AI worker's verdict.",
    "concept": "Asynchronous ML inference on the event stream (Isolation Forest)",
    "expected": [
        "The transfer commits without waiting for the model.",
        "Its event reaches the AI worker, which stores exactly one result.",
        "The verdict is reported with its score and threshold, not judged correct or wrong.",
    ],
    "how_it_works": [
        "Builds a short history for a new lab account (except the new-account scenario).",
        "Sends the scenario's final transfer through NGINX like any client.",
        "Waits for the asynchronous result in anomaly_results.",
    ],
    "estimated_seconds": 35,
}


def experiment_catalog() -> list[ExperimentDefinition]:
    safe = [
        ExperimentDefinition(
            **definition,
            risk_note=RISK_NOTE,
            available=True,
            params=param_specs(definition["id"]),
        )
        for definition in _DEFINITIONS
    ]
    # Listed so the UI can explain them, but never startable through the API.
    failures = [
        ExperimentDefinition(
            **definition,
            risk="infrastructure",
            runner="controller",
            risk_note=INFRA_RISK_NOTE,
            available=False,
            unavailable_reason=CONTROLLER_ONLY,
            params=param_specs(definition["id"]),
        )
        for definition in _FAILURE_DEFINITIONS
    ]
    ai = [
        ExperimentDefinition(
            **_AI_DEFINITION,
            runner="ai",
            risk_note=RISK_NOTE,
            available=True,
            params=param_specs("anomaly_scenario"),
        )
    ]
    return safe + failures + ai


def failure_definitions() -> list[dict[str, Any]]:
    return list(_FAILURE_DEFINITIONS)
