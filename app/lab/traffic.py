"""HTTP traffic for experiments: send a request, observe everything the response says.

Shared with the CLI demos (scripts/demo_common.py imports RETRYABLE_CODES and
transfer_body from here), so the browser lab and the terminal demos follow the same
retry contract.
"""

import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app.lab.schemas import RequestOutcome

# Safe to retry with the SAME request (and the same Idempotency-Key): nothing executed,
# or the outcome is unknown and the key makes the retry safe.
RETRYABLE_CODES = frozenset(
    {"CONCURRENCY_CONFLICT", "IDEMPOTENCY_REQUEST_IN_PROGRESS", "OUTCOME_UNKNOWN", "RATE_LIMITED"}
)
RETRYABLE_OUTCOMES = frozenset(
    {
        "rate_limited",
        "limiter_unavailable",
        "in_progress",
        "conflict",
        "server_error",
        "transport_error",
    }
)
TRANSFERS_PATH = "/api/v1/transfers"


def transfer_body(source: str, destination: str, amount_minor: int) -> dict[str, Any]:
    return {
        "source_account_id": source,
        "destination_account_id": destination,
        "amount_minor": amount_minor,
        "currency": "USD",
    }


def error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error.get("code") if isinstance(error, dict) else None


def classify(status: int | None, code: str | None, replayed: bool, method: str) -> RequestOutcome:
    if status is None:
        return "transport_error"
    if status in (200, 201):
        if method == "GET":
            return "read_ok"
        return "replayed" if replayed else "committed"
    by_code: dict[str, RequestOutcome] = {
        "INSUFFICIENT_FUNDS": "insufficient_funds",
        "RATE_LIMITED": "rate_limited",
        "IDEMPOTENCY_REQUEST_IN_PROGRESS": "in_progress",
        "CONCURRENCY_CONFLICT": "conflict",
        "IDEMPOTENCY_KEY_REUSED": "key_reused",
        "RATE_LIMITER_UNAVAILABLE": "limiter_unavailable",
    }
    if code in by_code:
        return by_code[code]
    if status == 429:
        return "rate_limited"
    return "server_error" if status >= 500 else "rejected"


def _int_header(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


@dataclass(slots=True)
class Observation:
    """One HTTP exchange as the lab saw it. Becomes a `request` event in the timeline."""

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
    # Not part of the event payload: used for checks inside the experiment.
    body: bytes = b""
    json: Any = None

    @property
    def retryable(self) -> bool:
        return self.outcome in RETRYABLE_OUTCOMES

    def event_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("body")
        payload.pop("json")
        return payload


class LabHttp:
    """An httpx.AsyncClient bound to one run: every request carries the run's signed
    lab-traffic token and is turned into an Observation (transport errors included)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(
        self,
        method: str,
        path: str,
        *,
        index: int,
        lane: str,
        attempt: int = 1,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Observation:
        started = time.perf_counter()
        try:
            response = await self._client.request(method, path, json=json, headers=headers)
        except httpx.TransportError as exc:
            return Observation(
                index=index,
                attempt=attempt,
                lane=lane,
                method=method,
                path=path,
                status=None,
                code=type(exc).__name__,
                outcome="transport_error",
                replica=None,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                replayed=False,
                retry_after_s=None,
                rate_policy=None,
                rate_remaining=None,
                request_id=None,
            )
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        replayed = response.headers.get("Idempotent-Replayed") == "true"
        code = None if response.is_success else error_code(response)
        try:
            body_json = response.json()
        except ValueError:
            body_json = None
        return Observation(
            index=index,
            attempt=attempt,
            lane=lane,
            method=method,
            path=path,
            status=response.status_code,
            code=code,
            outcome=classify(response.status_code, code, replayed, method),
            replica=response.headers.get("X-Served-By"),
            latency_ms=latency_ms,
            replayed=replayed,
            retry_after_s=_int_header(response, "Retry-After"),
            rate_policy=response.headers.get("X-RateLimit-Policy"),
            rate_remaining=_int_header(response, "X-RateLimit-Remaining"),
            request_id=response.headers.get("X-Request-ID"),
            body=response.content,
            json=body_json,
        )


def retry_delay(observation: Observation) -> float:
    """Seconds to wait before retrying: Retry-After when the server sent one (capped)."""
    if observation.retry_after_s is not None:
        return float(min(observation.retry_after_s, 5))
    return 0.2
