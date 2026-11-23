"""Experiment Lab API contract.

Every experiment has a parameter model with hard bounds (`extra="forbid"`, strict
integers). A run request is a discriminated union over the allow-listed experiment ids,
so an unknown experiment or an unknown/out-of-range parameter is a 422 before anything
runs. There is no way to name code, commands, hosts or files.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ExperimentId = Literal[
    "concurrency",
    "idempotency",
    "rate_limiting",
    "load_balancing",
    # Phase 5D: controlled failures. Runnable ONLY by the local experiment controller
    # (labctl); the API's run-creation schema below does not accept them.
    "redis_outage",
    "rabbitmq_outage",
    "worker_crash",
    "replica_failure",
    # Phase 5E: AI Lab scenarios (traffic only; started from the AI Lab page).
    "anomaly_scenario",
]
# Experiments the API itself may start (no infrastructure control).
API_EXPERIMENTS = ("concurrency", "idempotency", "rate_limiting", "load_balancing")
RunStatus = Literal["queued", "running", "passed", "failed", "error"]
CheckStatus = Literal["pass", "fail", "skipped"]
EventKind = Literal["phase", "request", "check", "note"]
EventLevel = Literal["info", "success", "warning", "error"]
# What one observed HTTP request turned out to be; the UI derives the request path from it.
RequestOutcome = Literal[
    "committed",  # 201, money moved (first execution)
    "replayed",  # 201 + Idempotent-Replayed: the stored outcome, no new effect
    "insufficient_funds",  # 409, final business rejection after locking the rows
    "rate_limited",  # 429 from the token bucket; nothing executed
    "limiter_unavailable",  # 503 RATE_LIMITER_UNAVAILABLE: Redis down, write failed closed
    "in_progress",  # 409 IDEMPOTENCY_REQUEST_IN_PROGRESS; same key still running
    "conflict",  # 503 CONCURRENCY_CONFLICT; lock wait timed out, rolled back
    "key_reused",  # 422 IDEMPOTENCY_KEY_REUSED; same key, different body
    "read_ok",  # 200 for a read
    "rejected",  # any other 4xx business/validation answer
    "server_error",  # 5xx (including OUTCOME_UNKNOWN, 502/504 from NGINX)
    "transport_error",  # no HTTP response at all
]


def _int(default: int, ge: int, le: int, title: str, description: str, unit: str) -> Any:
    return Field(
        default=default,
        ge=ge,
        le=le,
        strict=True,
        title=title,
        description=description,
        json_schema_extra={"unit": unit},
    )


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConcurrencyParams(_Params):
    requests: int = _int(
        100, 2, 200, "Concurrent requests", "Transfers released at the same instant.", "requests"
    )
    amount_minor: int = _int(
        1_000, 1, 100_000, "Amount per transfer", "Each withdrawal, in cents.", "cents"
    )
    funding_minor: int = _int(
        10_000,
        1,
        1_000_000,
        "Starting balance",
        "Simulated money minted into the isolated source account, in cents.",
        "cents",
    )


class IdempotencyParams(_Params):
    duplicates: int = _int(
        20,
        2,
        100,
        "Duplicate requests",
        "Concurrent copies of ONE transfer, all with the same Idempotency-Key.",
        "requests",
    )
    amount_minor: int = _int(
        1_000, 1, 100_000, "Transfer amount", "The single logical transfer, in cents.", "cents"
    )


class RateLimitingParams(_Params):
    burst_requests: int = _int(
        50,
        5,
        150,
        "Burst size",
        "Concurrent write requests fired at once against a fresh bucket.",
        "requests",
    )
    refill_wait_seconds: int = _int(
        2, 0, 5, "Refill wait", "Pause after the burst so the bucket can refill.", "seconds"
    )
    probe_requests: int = _int(
        30,
        0,
        60,
        "Requests after the wait",
        "Second burst that measures how many tokens came back (0 = skip).",
        "requests",
    )


class LoadBalancingParams(_Params):
    requests: int = _int(40, 2, 200, "Requests", "Read requests sent through NGINX.", "requests")
    concurrency: int = _int(
        1,
        1,
        20,
        "In flight at once",
        "1 sends them one after another, which shows the round-robin pattern most clearly.",
        "requests",
    )


# --- Phase 5D failure experiments (local controller only) -------------------------------


class RedisOutageParams(_Params):
    probe_writes: int = _int(
        3, 1, 5, "Writes during the outage", "Transfers attempted while Redis is down.", "requests"
    )
    outage_seconds: int = _int(
        3, 1, 15, "Outage length", "How long Redis stays stopped (at least the probes).", "seconds"
    )


class RabbitMqOutageParams(_Params):
    transfers: int = _int(
        3, 1, 10, "Transfers during the outage", "Committed while RabbitMQ is down.", "requests"
    )
    outage_seconds: int = _int(
        5, 2, 20, "Outage length", "How long RabbitMQ stays stopped.", "seconds"
    )


class WorkerCrashParams(_Params):
    amount_minor: int = _int(
        100, 1, 10_000, "Transfer amount", "One transfer per crash scenario, in cents.", "cents"
    )


class ReplicaFailureParams(_Params):
    requests_per_phase: int = _int(
        12,
        4,
        40,
        "Reads per phase",
        "Sequential reads before, during and after the replica is stopped.",
        "requests",
    )


class AnomalyScenarioParams(_Params):
    scenario: int = _int(
        0,
        0,
        3,
        "Scenario",
        "0 normal, 1 unusually large amount, 2 rapid burst, 3 new-account unusual activity.",
        "requests",
    )


PARAMS_BY_EXPERIMENT: dict[str, type[_Params]] = {
    "concurrency": ConcurrencyParams,
    "idempotency": IdempotencyParams,
    "rate_limiting": RateLimitingParams,
    "load_balancing": LoadBalancingParams,
    "redis_outage": RedisOutageParams,
    "rabbitmq_outage": RabbitMqOutageParams,
    "worker_crash": WorkerCrashParams,
    "replica_failure": ReplicaFailureParams,
    "anomaly_scenario": AnomalyScenarioParams,
}


# --- run creation ---------------------------------------------------------------------


class _RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConcurrencyRunCreate(_RunCreate):
    experiment: Literal["concurrency"]
    params: ConcurrencyParams = ConcurrencyParams()


class IdempotencyRunCreate(_RunCreate):
    experiment: Literal["idempotency"]
    params: IdempotencyParams = IdempotencyParams()


class RateLimitingRunCreate(_RunCreate):
    experiment: Literal["rate_limiting"]
    params: RateLimitingParams = RateLimitingParams()


class LoadBalancingRunCreate(_RunCreate):
    experiment: Literal["load_balancing"]
    params: LoadBalancingParams = LoadBalancingParams()


class AnomalyScenarioRunCreate(_RunCreate):
    experiment: Literal["anomaly_scenario"]
    params: AnomalyScenarioParams = AnomalyScenarioParams()


LabRunCreate = Annotated[
    ConcurrencyRunCreate
    | IdempotencyRunCreate
    | RateLimitingRunCreate
    | LoadBalancingRunCreate
    | AnomalyScenarioRunCreate,
    Field(discriminator="experiment"),
]


# --- catalog ----------------------------------------------------------------------------


class ParamSpec(BaseModel):
    name: str
    label: str
    description: str
    unit: Literal["requests", "cents", "seconds"]
    default: int
    minimum: int
    maximum: int


class ExperimentDefinition(BaseModel):
    id: ExperimentId
    title: str
    summary: str
    concept: str
    expected: list[str]
    how_it_works: list[str]
    # safe_local: bounded traffic only. infrastructure: stops/restarts an allow-listed
    # local service, so it runs only through the local experiment controller.
    risk: Literal["safe_local", "infrastructure"] = "safe_local"
    # api: Experiment Lab; ai: AI Lab page (also started by the API); controller: labctl only.
    runner: Literal["api", "ai", "controller"] = "api"
    risk_note: str
    available: bool
    unavailable_reason: str | None = None
    estimated_seconds: int
    params: list[ParamSpec]


# --- run state ------------------------------------------------------------------------


class LabProgress(BaseModel):
    phase: str = "queued"
    message: str = ""
    completed: int = 0
    total: int = 0


class LabMetric(BaseModel):
    key: str
    label: str
    value: int | float | str
    unit: str | None = None
    tone: Literal["neutral", "success", "warning", "destructive", "info"] = "neutral"
    hint: str | None = None


class LabCheck(BaseModel):
    id: str
    label: str
    status: CheckStatus
    observed: str
    description: str | None = None


class LabRunResult(BaseModel):
    summary: str
    explanation: list[str]
    metrics: list[LabMetric]
    outcome_counts: dict[str, int]
    data: dict[str, Any]


class LabRunRead(BaseModel):
    id: str
    experiment: ExperimentId
    status: RunStatus
    params: dict[str, int]
    progress: LabProgress
    result: LabRunResult | None
    checks: list[LabCheck] | None
    error: str | None
    runner_replica: str
    target_base_url: str
    event_count: int
    events_dropped: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime


class LabRunSummary(BaseModel):
    id: str
    experiment: ExperimentId
    status: RunStatus
    params: dict[str, int]
    summary: str | None
    checks_passed: int
    checks_total: int
    created_at: datetime
    finished_at: datetime | None


class LabRequestObservation(BaseModel):
    index: int
    attempt: int
    lane: str
    method: str
    path: str
    status: int | None
    code: str | None
    outcome: RequestOutcome
    replica: str | None
    latency_ms: float
    replayed: bool
    retry_after_s: int | None
    rate_policy: str | None
    rate_remaining: int | None
    request_id: str | None


class LabEvent(BaseModel):
    seq: int
    t_ms: int
    kind: EventKind
    level: EventLevel
    title: str
    detail: str | None = None
    phase: str | None = None
    request: LabRequestObservation | None = None


class LabEventPage(BaseModel):
    items: list[LabEvent]
    next_after: int
    run_status: RunStatus
    event_count: int
    events_dropped: int
