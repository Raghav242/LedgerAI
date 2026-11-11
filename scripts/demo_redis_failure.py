"""Redis outage demo: financial writes fail CLOSED, reads stay up, recovery is clean.

LOCAL ONLY: stops and restarts the local stack's Redis container.

    python scripts/demo_redis_failure.py
"""

import time
import uuid

import httpx
from demo_common import (
    balance,
    base_arg_parser,
    check,
    create_demo_account,
    error_code,
    fund,
    new_run_id,
    preflight,
    transfer_body,
    usd,
    verify_ledger,
)
from stack_control import compose, ensure_local, wait_ready

AMOUNT = 2_500


def history_count(client: httpx.Client, account_id: str) -> int:
    response = client.get(
        "/api/v1/transfers", params={"account_id": account_id, "kind": "transfer"}
    )
    return len(response.json()["items"])


def main() -> None:
    args = base_arg_parser(__doc__ or "").parse_args()
    ensure_local(args.base_url)
    wait_ready(args.base_url)
    key = str(uuid.uuid4())

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        preflight(client)
        run_id = new_run_id()
        source = create_demo_account(client, run_id, "source")
        destination = create_demo_account(client, run_id, "destination")
        fund(client, source, 10_000)
        body = transfer_body(source, destination, AMOUNT)
        before = (balance(client, source), balance(client, destination))
        print(f"Run {run_id}: source {usd(before[0])}, destination {usd(before[1])}")

        print("\n1) Stopping Redis (docker compose stop redis) ...")
        compose("stop", "redis")
        try:
            started = time.monotonic()
            write = client.post("/api/v1/transfers", json=body, headers={"Idempotency-Key": key})
            latency_ms = (time.monotonic() - started) * 1000
            print(
                f"  POST /transfers -> {write.status_code} {error_code(write)} "
                f"(Retry-After {write.headers.get('Retry-After')}) in {latency_ms:.0f} ms"
            )
            check(write.status_code == 503, "financial write refused with 503")
            check(error_code(write) == "RATE_LIMITER_UNAVAILABLE", "stable error code")
            check(latency_ms < 2_000, "failure is fast (strict Redis timeouts), not a hang")

            during = (balance(client, source), balance(client, destination))
            print(
                f"  balances (reads fail OPEN): source {usd(during[0])}, "
                f"destination {usd(during[1])}"
            )
            check(during == before, "no financial state changed")
            check(history_count(client, destination) == 0, "no transfer recorded")
            live = client.get("/health/live")
            ready = client.get("/health/ready")
            print(
                f"  /health/live -> {live.status_code}   /health/ready -> {ready.status_code} "
                f"{ready.json()}"
            )
            check(live.status_code == 200, "liveness unaffected (replicas are not killed)")
            check(
                ready.status_code == 503 and ready.json()["redis"] == "unavailable",
                "readiness reports the Redis outage",
            )
        finally:
            print("\n2) Starting Redis again ...")
            compose("start", "redis")
            wait_ready(args.base_url)

        retry = client.post("/api/v1/transfers", json=body, headers={"Idempotency-Key": key})
        print(f"  retry with the SAME Idempotency-Key -> {retry.status_code}")
        check(retry.status_code == 201, "API recovered: the transfer now executes")
        check(
            "Idempotent-Replayed" not in retry.headers,
            "first real execution (the refused attempt left no idempotency record)",
        )
        after = (balance(client, source), balance(client, destination))
        print(f"  balances: source {usd(after[0])}, destination {usd(after[1])}")
        check(after == (before[0] - AMOUNT, before[1] + AMOUNT), "money moved exactly once")
        verify_ledger(client, [source, destination])
    print("\nPASS: fail-closed writes, fail-open reads, clean recovery.")


if __name__ == "__main__":
    main()
