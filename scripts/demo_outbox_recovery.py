"""Outbox recovery demo: RabbitMQ goes down, money keeps moving, no event is lost.

LOCAL ONLY: stops and restarts the stack's RabbitMQ container (always restarted at exit).

    python scripts/demo_outbox_recovery.py [--transfers 5]
"""

import time
import uuid

import httpx
from demo_common import (
    base_arg_parser,
    check,
    create_demo_account,
    fund,
    new_run_id,
    post_admitted,
    preflight,
    transfer_body,
    verify_ledger,
)
from stack_control import compose, db_query, ensure_local, wait_ready, wait_until

from app.events.ids import event_id_for
from app.events.schemas import TRANSFER_COMPLETED


def outbox_states(event_ids: list[uuid.UUID]) -> dict[str, int]:
    rows = db_query(
        "SELECT status, count(*) FROM outbox_events WHERE id = ANY(:ids) GROUP BY 1",
        {"ids": event_ids},
    )
    return {status: count for status, count in rows}


def effects(event_ids: list[uuid.UUID]) -> tuple[int, int]:
    (row,) = db_query(
        "SELECT (SELECT count(*) FROM processed_events WHERE event_id = ANY(:ids)), "
        "(SELECT count(*) FROM event_audit_log WHERE event_id = ANY(:ids))",
        {"ids": event_ids},
    )
    return row[0], row[1]


def main() -> None:
    parser = base_arg_parser(__doc__ or "")
    parser.add_argument("--transfers", type=int, default=5)
    args = parser.parse_args()
    ensure_local(args.base_url)
    wait_ready(args.base_url)

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        preflight(client)
        run_id = new_run_id()
        source = create_demo_account(client, run_id, "source")
        destination = create_demo_account(client, run_id, "destination")
        fund(client, source, 100_000)

        print("1) Stopping RabbitMQ (docker compose stop rabbitmq) ...")
        compose("stop", "rabbitmq")
        try:
            event_ids = []
            for _ in range(args.transfers):
                response = post_admitted(
                    client,
                    "/api/v1/transfers",
                    json=transfer_body(source, destination, 100),
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                )
                check(response.status_code == 201, "transfer committed while the broker is down")
                event_ids.append(event_id_for(TRANSFER_COMPLETED, uuid.UUID(response.json()["id"])))
            print(f"   {args.transfers} transfers committed; the API never talks to RabbitMQ.")
            for second in range(3):
                time.sleep(1)
                print(
                    f"   t+{second + 1}s outbox for these events: {outbox_states(event_ids)} "
                    f"(receipts, audit rows) = {effects(event_ids)}"
                )
            check(
                outbox_states(event_ids) == {"pending": args.transfers},
                "events wait safely in the outbox (nothing published, nothing lost)",
            )
            logs = compose("logs", "--since", "30s", "outbox-publisher").stdout
            retries = logs.count("cannot make progress")
            print(
                f"   publisher log: {retries} 'cannot make progress ... retrying in N ms' lines "
                "(exponential backoff with jitter)"
            )
        finally:
            print("\n2) Starting RabbitMQ again ...")
            compose("start", "rabbitmq")

        started = time.monotonic()
        wait_until(
            lambda: effects(event_ids) == (args.transfers, args.transfers),
            timeout=120,
            what="all events delivered and processed",
        )
        print(f"   delivered and processed {time.monotonic() - started:.1f}s after the restart")
        print(
            f"   outbox: {outbox_states(event_ids)}   (receipts, audit rows) = {effects(event_ids)}"
        )
        check(outbox_states(event_ids) == {"published": args.transfers}, "every event published")
        check(
            effects(event_ids) == (args.transfers, args.transfers),
            "every event processed exactly once (one receipt, one audit row each)",
        )
        verify_ledger(client, [source, destination])
    print("\nPASS: broker outage delayed events but lost none; money movement never stopped.")


if __name__ == "__main__":
    main()
