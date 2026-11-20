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


def experiment_catalog() -> list[ExperimentDefinition]:
    return [
        ExperimentDefinition(
            **definition,
            risk_note=RISK_NOTE,
            available=True,
            params=param_specs(definition["id"]),
        )
        for definition in _DEFINITIONS
    ]
