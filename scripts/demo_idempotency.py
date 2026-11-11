"""Idempotency demo: the same transfer sent 20 times at once with ONE Idempotency-Key.

Simulates a client (or proxy) retrying aggressively. Exactly one business effect must be
committed; every response must describe that same transfer. Exits with status 1 otherwise.

    python scripts/demo_idempotency.py [--base-url http://127.0.0.1:8000] [--duplicates 20]
"""

import asyncio
import uuid

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

FUNDING_MINOR = 10_000
AMOUNT_MINOR = 1_000
MAX_ATTEMPTS = 5


async def send(
    client: httpx.AsyncClient, start: asyncio.Event, body: dict, key: str
) -> httpx.Response:
    await start.wait()
    response = None
    for _ in range(MAX_ATTEMPTS):
        response = await client.post(
            "/api/v1/transfers", json=body, headers={"Idempotency-Key": key}
        )
        if error_code(response) not in RETRYABLE_CODES:
            break
    assert response is not None
    return response


async def fire(base_url: str, duplicates: int, body: dict, key: str) -> list[httpx.Response]:
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        start = asyncio.Event()
        tasks = [asyncio.create_task(send(client, start, body, key)) for _ in range(duplicates)]
        await asyncio.sleep(0)
        start.set()
        return await asyncio.gather(*tasks)


def main() -> None:
    parser = base_arg_parser(__doc__ or "")
    parser.add_argument("--duplicates", type=int, default=20)
    args = parser.parse_args()
    run_id = new_run_id()
    key = f"demo-{run_id}-order-1"

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        preflight(client)
        source = create_demo_account(client, run_id, "source")
        destination = create_demo_account(client, run_id, "destination")
        fund(client, source, FUNDING_MINOR)
        body = transfer_body(source, destination, AMOUNT_MINOR)
        print(
            f"Run {run_id}: sending {args.duplicates} concurrent copies of one "
            f"{usd(AMOUNT_MINOR)} transfer with Idempotency-Key={key}"
        )

        responses = asyncio.run(fire(args.base_url, args.duplicates, body, key))

        statuses = sorted({r.status_code for r in responses})
        originals = [r for r in responses if "Idempotent-Replayed" not in r.headers]
        replays = [r for r in responses if r.headers.get("Idempotent-Replayed") == "true"]
        transfer_ids = {r.json().get("id") for r in responses if r.status_code == 201}
        print("\nResults")
        print(f"  HTTP statuses       : {statuses}")
        print(f"  original executions : {len(originals)}")
        print(f"  replayed responses  : {len(replays)}")
        print(f"  distinct transfer ids in responses: {transfer_ids}")

        source_balance, destination_balance = balance(client, source), balance(client, destination)
        history = client.get(
            "/api/v1/transfers", params={"account_id": source, "kind": "transfer"}
        ).json()["items"]
        print("\nFinal balances")
        print(f"  source      : {usd(source_balance)}")
        print(f"  destination : {usd(destination_balance)}")

        print("\nInvariants")
        check(statuses == [201], "every duplicate got 201")
        check(len(originals) == 1, "exactly one request executed the transfer")
        check(len(replays) == args.duplicates - 1, "all others were replays")
        check(len(transfer_ids) == 1, "all responses describe the same transfer")
        check(len({r.content for r in responses}) == 1, "all response bodies are byte-identical")
        check(len(history) == 1, "exactly one transfer recorded")
        check(source_balance == FUNDING_MINOR - AMOUNT_MINOR, "source debited exactly once")
        check(destination_balance == AMOUNT_MINOR, "destination credited exactly once")

        reuse = client.post(
            "/api/v1/transfers",
            json=transfer_body(source, destination, AMOUNT_MINOR * 2),
            headers={"Idempotency-Key": key},
        )
        check(
            reuse.status_code == 422 and error_code(reuse) == "IDEMPOTENCY_KEY_REUSED",
            "same key with a different amount is rejected (422 IDEMPOTENCY_KEY_REUSED)",
        )
        check(balance(client, source) == source_balance, "rejected reuse moved no money")
        fresh = client.post(
            "/api/v1/transfers", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}
        )
        check(fresh.status_code == 201, "a NEW key is a new, separate transfer")
        verify_ledger(client, [source, destination])
    print("\nPASS: one key, one committed business effect.")


if __name__ == "__main__":
    main()
