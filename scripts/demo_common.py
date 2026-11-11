"""Shared helpers for the demo scripts. They talk to a running API over HTTP.

Isolation: every run creates its own uniquely named accounts ("demo-<run>-...") and only
ever moves money between those, so existing merchant data is never touched. Funding is
drawn from the system funding account, which every funding uses by design.
"""

import argparse
import sys
import time
import uuid
from typing import Any, NoReturn

import httpx

# Safe to retry with the SAME request (and the same Idempotency-Key): nothing executed,
# or the outcome is unknown and the key makes the retry safe.
RETRYABLE_CODES = {
    "CONCURRENCY_CONFLICT",
    "IDEMPOTENCY_REQUEST_IN_PROGRESS",
    "OUTCOME_UNKNOWN",
    "RATE_LIMITED",
}
MAX_ATTEMPTS = 120
DIRECT_API_URL = "http://127.0.0.1:8000"  # a locally run uvicorn
NGINX_URL = "http://127.0.0.1:8080"  # the Compose stack's load balancer


def base_arg_parser(description: str, default_url: str = NGINX_URL) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--base-url", default=default_url, help=f"API base URL ({default_url})")
    return parser


def retry_delay(response: httpx.Response | None) -> float:
    """Seconds to wait before retrying: Retry-After when the server sent one."""
    if response is not None and "Retry-After" in response.headers:
        return float(response.headers["Retry-After"])
    return 0.2


def post_admitted(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    """POST, retrying only on 429: a rate-limited request was never executed, so even a
    non-idempotent call (like dev funding) is safe to resend."""
    for _ in range(MAX_ATTEMPTS):
        response = client.post(url, **kwargs)
        if response.status_code != 429:
            return response
        time.sleep(retry_delay(response))
    return response


def fail(message: str) -> NoReturn:
    print(f"\nFAIL: {message}")
    sys.exit(1)


def check(condition: bool, message: str) -> None:
    print(f"  [{'ok' if condition else 'FAIL'}] {message}")
    if not condition:
        fail(message)


def preflight(client: httpx.Client) -> None:
    try:
        health = client.get("/health")
    except httpx.HTTPError as exc:
        fail(f"API not reachable ({exc}). Start it with: uvicorn app.main:create_app --factory")
    if health.status_code != 200:
        fail(f"API unhealthy: {health.status_code} {health.text}")
    paths = client.get("/openapi.json").json()["paths"]
    if "/api/v1/accounts/{account_id}/fund" not in paths:
        fail("The dev funding endpoint is disabled. Set ENABLE_DEV_ENDPOINTS=true and restart.")


def create_demo_account(client: httpx.Client, run_id: str, label: str) -> str:
    response = post_admitted(client, "/api/v1/accounts", json={"name": f"demo-{run_id}-{label}"})
    response.raise_for_status()
    return response.json()["id"]


def fund(client: httpx.Client, account_id: str, amount_minor: int) -> None:
    response = post_admitted(
        client, f"/api/v1/accounts/{account_id}/fund", json={"amount_minor": amount_minor}
    )
    response.raise_for_status()


def balance(client: httpx.Client, account_id: str) -> int:
    response = client.get(f"/api/v1/accounts/{account_id}/balance")
    response.raise_for_status()
    return response.json()["balance_minor"]


def transfer_body(source: str, destination: str, amount_minor: int) -> dict[str, Any]:
    return {
        "source_account_id": source,
        "destination_account_id": destination,
        "amount_minor": amount_minor,
        "currency": "USD",
    }


def error_code(response: httpx.Response) -> str | None:
    try:
        return response.json().get("error", {}).get("code")
    except ValueError:
        return None


def verify_ledger(client: httpx.Client, account_ids: list[str]) -> None:
    """Per-account ledger balances for this run's accounts + global reconciliation."""
    report = client.get("/api/v1/reconciliation").json()
    mismatched = {m["account_id"] for m in report["balance_mismatches"]}
    for account_id in account_ids:
        check(account_id not in mismatched, f"ledger balance == stored balance for {account_id}")
    check(report["ledger_total_minor"] == 0, "all ledger entries sum to zero")
    check(report["balance_total_minor"] == 0, "all account balances (incl. system) sum to zero")
    check(not report["unbalanced_transfer_ids"], "every transfer has exactly two balanced entries")
    check(report["ok"], "global reconciliation ok")


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def usd(minor: int) -> str:
    return f"${minor / 100:,.2f}"
