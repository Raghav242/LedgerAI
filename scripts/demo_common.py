"""Shared helpers for the demo scripts. They talk to a running API over HTTP.

Isolation: every run creates its own uniquely named accounts ("demo-<run>-...") and only
ever moves money between those, so existing merchant data is never touched. Funding is
drawn from the system funding account, which every funding uses by design.
"""

import argparse
import sys
import uuid
from typing import Any, NoReturn

import httpx

RETRYABLE_CODES = {"CONCURRENCY_CONFLICT", "IDEMPOTENCY_REQUEST_IN_PROGRESS", "OUTCOME_UNKNOWN"}


def base_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base URL")
    return parser


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
    response = client.post("/api/v1/accounts", json={"name": f"demo-{run_id}-{label}"})
    response.raise_for_status()
    return response.json()["id"]


def fund(client: httpx.Client, account_id: str, amount_minor: int) -> None:
    response = client.post(
        f"/api/v1/accounts/{account_id}/fund", json={"amount_minor": amount_minor}
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
