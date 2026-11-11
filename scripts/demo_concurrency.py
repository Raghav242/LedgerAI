"""Concurrency demo: 100 simultaneous $10 transfers from an account holding $100.

Exactly 10 must succeed and 90 must be rejected for insufficient funds, whatever order
the server processes them in. Exits with status 1 if any invariant is violated.

    python scripts/demo_concurrency.py [--base-url http://127.0.0.1:8000] [--requests 100]
"""

import asyncio
import uuid
from collections import Counter

import httpx
from demo_common import (
    RETRYABLE_CODES,
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

FUNDING_MINOR = 10_000  # $100.00
AMOUNT_MINOR = 1_000  # $10.00
MAX_ATTEMPTS = 5


async def send_transfer(
    client: httpx.AsyncClient, start: asyncio.Event, source: str, destination: str
) -> str:
    """Send one transfer; on a retryable failure retry with the SAME key (never a new one)."""
    key = str(uuid.uuid4())
    await start.wait()
    for _ in range(MAX_ATTEMPTS):
        try:
            response = await client.post(
                "/api/v1/transfers",
                json=transfer_body(source, destination, AMOUNT_MINOR),
                headers={"Idempotency-Key": key},
            )
        except httpx.TransportError:
            continue  # outcome unknown: the same key makes the retry safe
        code = error_code(response)
        if response.status_code == 201:
            return "succeeded"
        if code == "INSUFFICIENT_FUNDS":
            return "insufficient_funds"
        if code not in RETRYABLE_CODES:
            return f"unexpected:{response.status_code}:{code}"
    return "unexpected:retries_exhausted"


async def fire(base_url: str, requests: int, source: str, destination: str) -> Counter[str]:
    limits = httpx.Limits(max_connections=requests, max_keepalive_connections=requests)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=60) as client:
        start = asyncio.Event()
        tasks = [
            asyncio.create_task(send_transfer(client, start, source, destination))
            for _ in range(requests)
        ]
        await asyncio.sleep(0)  # let every task reach start.wait() before releasing them
        start.set()
        return Counter(await asyncio.gather(*tasks))


def main() -> None:
    parser = base_arg_parser(__doc__ or "")
    parser.add_argument("--requests", type=int, default=100)
    args = parser.parse_args()
    run_id = new_run_id()

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        preflight(client)
        source = create_demo_account(client, run_id, "source")
        destination = create_demo_account(client, run_id, "destination")
        fund(client, source, FUNDING_MINOR)
        print(f"Run {run_id}: source {source} funded with {usd(FUNDING_MINOR)}")
        print(f"Sending {args.requests} concurrent transfers of {usd(AMOUNT_MINOR)} ...")

        counts = asyncio.run(fire(args.base_url, args.requests, source, destination))

        succeeded = counts["succeeded"]
        insufficient = counts["insufficient_funds"]
        unexpected = {k: v for k, v in counts.items() if k.startswith("unexpected")}
        print("\nResults")
        print(f"  successful transfers : {succeeded}")
        print(f"  insufficient funds   : {insufficient}")
        print(f"  unexpected errors    : {sum(unexpected.values())} {unexpected or ''}")

        source_balance, destination_balance = balance(client, source), balance(client, destination)
        print("\nFinal balances")
        print(f"  source      : {usd(source_balance)}")
        print(f"  destination : {usd(destination_balance)}")

        expected_successes = min(args.requests, FUNDING_MINOR // AMOUNT_MINOR)
        print("\nInvariants")
        check(not unexpected, "no unexpected errors")
        check(succeeded == expected_successes, f"exactly {expected_successes} transfers succeeded")
        check(insufficient == args.requests - expected_successes, "all others: insufficient funds")
        check(source_balance >= 0, "source balance never negative")
        check(source_balance + destination_balance == FUNDING_MINOR, "money conserved")
        check(
            destination_balance == succeeded * AMOUNT_MINOR, "destination got exactly the successes"
        )
        verify_ledger(client, [source, destination])
    print("\nPASS: no overdraft, no lost update, ledger balanced.")


if __name__ == "__main__":
    main()
