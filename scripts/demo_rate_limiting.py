"""Distributed rate-limiting demo through NGINX (two replicas, one shared Redis quota).

LOCAL ONLY: temporarily redeploys the API replicas with a small write quota
(10 burst, 1 token/second) and restores the default configuration afterwards.

    python scripts/demo_rate_limiting.py [--requests 50]
"""

import math
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
from demo_common import (
    balance,
    base_arg_parser,
    check,
    create_demo_account,
    fund,
    new_run_id,
    preflight,
    transfer_body,
    verify_ledger,
)
from stack_control import deploy, ensure_local, reset_rate_limits, restore_default, wait_ready

from app.rate_limiting import InMemoryFixedWindowLimiter, RateLimitPolicy

CAPACITY = 10
REFILL_TOKENS = 1
REFILL_INTERVAL_MS = 1000
DEMO_QUOTA = {
    "ENABLE_DEV_ENDPOINTS": "true",
    "EXPOSE_REPLICA_HEADER": "true",
    "RATE_LIMIT_WRITE_CAPACITY": str(CAPACITY),
    "RATE_LIMIT_WRITE_REFILL_TOKENS": str(REFILL_TOKENS),
    "RATE_LIMIT_WRITE_REFILL_INTERVAL_MS": str(REFILL_INTERVAL_MS),
}
POLICY = RateLimitPolicy("write", CAPACITY, REFILL_TOKENS, REFILL_INTERVAL_MS)


def send_transfers(
    client: httpx.Client, source: str, destination: str, n: int
) -> list[httpx.Response]:
    """n concurrent transfers over ONE shared, thread-safe client (connection pool)."""

    def one(_: int) -> httpx.Response:
        return client.post(
            "/api/v1/transfers",
            json=transfer_body(source, destination, 100),
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(one, range(n)))


def send_until_rejected(client: httpx.Client, source: str, destination: str) -> tuple[int, float]:
    """Sequential transfers until the first 429. Returns (#admitted, time of the 429)."""
    admitted = 0
    while True:
        (response,) = send_transfers(client, source, destination, 1)
        if response.status_code == 429:
            return admitted, time.monotonic()
        admitted += 1


def per_replica_counterfactual(responses: list[httpx.Response]) -> int:
    """How many of the same requests independent per-replica limiters would admit."""
    clock_ms = 0  # the whole burst falls into one fixed window
    limiters: dict[str, InMemoryFixedWindowLimiter] = {}
    admitted = 0
    for response in responses:
        replica = response.headers.get("X-Served-By", "?")
        limiter = limiters.setdefault(replica, InMemoryFixedWindowLimiter(lambda: clock_ms))
        admitted += limiter.allow_request("demo-merchant", POLICY).allowed
    return admitted


def main() -> None:
    parser = base_arg_parser(__doc__ or "")
    parser.add_argument("--requests", type=int, default=50)
    args = parser.parse_args()
    ensure_local(args.base_url)

    print(
        f"Redeploying API replicas with a demo write quota: burst {CAPACITY}, "
        f"refill {REFILL_TOKENS} token / {REFILL_INTERVAL_MS} ms ..."
    )
    deploy(DEMO_QUOTA)
    try:
        wait_ready(args.base_url)
        limits = httpx.Limits(max_connections=args.requests)
        with httpx.Client(base_url=args.base_url, timeout=30, limits=limits) as client:
            preflight(client)
            run_id = new_run_id()
            source = create_demo_account(client, run_id, "source")
            destination = create_demo_account(client, run_id, "destination")
            fund(client, source, 1_000_000)
            reset_rate_limits()  # setup spent write tokens: start from a full bucket

            print(f"\n1) {args.requests} concurrent $1.00 transfers through NGINX")
            started = time.monotonic()
            responses = send_transfers(client, source, destination, args.requests)
            elapsed = time.monotonic() - started
            statuses = Counter(r.status_code for r in responses)
            by_replica = Counter((r.headers.get("X-Served-By"), r.status_code) for r in responses)
            allowed = statuses[201]
            refilled_max = math.floor(elapsed * 1000 * REFILL_TOKENS / REFILL_INTERVAL_MS) + 1
            print(f"  burst took {elapsed * 1000:.0f} ms")
            print(
                f"  allowed (201): {allowed}    rejected (429): {statuses[429]}    other: "
                f"{sum(v for k, v in statuses.items() if k not in (201, 429))}"
            )
            for (replica, status), n in sorted(by_replica.items()):
                print(f"    {replica}: {status} x {n}")
            counterfactual = per_replica_counterfactual(responses)
            print(
                f"  independent in-memory limiters per replica would have admitted: "
                f"{counterfactual}"
            )

            check(set(statuses) <= {201, 429}, "only 201 or 429 responses")
            check(
                CAPACITY <= allowed <= CAPACITY + refilled_max,
                f"global quota respected: {CAPACITY} burst (+ at most {refilled_max} refilled "
                "during the burst)",
            )
            check(
                {r.headers["X-Served-By"] for r in responses if r.status_code == 201}
                == {"api-1", "api-2"},
                "admitted requests were served by both replicas",
            )
            check(counterfactual > allowed, "per-replica counters would over-admit")
            first_429 = next(r for r in responses if r.status_code == 429)
            retry_after = int(first_429.headers["Retry-After"])
            print(f"  429 body: {first_429.json()['error']['code']}, Retry-After: {retry_after}s")
            moved = balance(client, destination)
            check(moved == allowed * 100, "money moved only for admitted requests")

            print("\n2) Token refill")
            drained, emptied_at = send_until_rejected(client, source, destination)
            print(f"  drained {drained} leftover token(s); bucket empty")
            time.sleep(3)
            refilled, ended_at = send_until_rejected(client, source, destination)
            window_s = ended_at - emptied_at
            expected_max = math.floor(window_s * 1000 * REFILL_TOKENS / REFILL_INTERVAL_MS)
            print(
                f"  after waiting 3 s: {refilled} request(s) admitted, then 429 "
                f"({window_s:.2f} s since the bucket was empty)"
            )
            check(
                3 <= refilled <= expected_max,
                f"refill matches 1 token/s: 3 <= {refilled} <= {expected_max}",
            )
            verify_ledger(client, [source, destination])
    finally:
        print("\nRestoring the default stack configuration ...")
        restore_default()
    print("\nPASS: one quota shared by every replica, enforced atomically in Redis.")


if __name__ == "__main__":
    main()
