"""Event deduplication demo: one event delivered many times, applied once.

Simulates both duplicate sources: the publisher re-publishing (a lost confirm / expired
lease) and an extra copy injected straight into the exchange. Only this demo's own event
is touched.

    python scripts/demo_event_deduplication.py [--copies 5]
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
from stack_control import (
    compose,
    db_execute,
    db_query,
    ensure_local,
    queue_depths,
    wait_ready,
    wait_until,
)

from app.core.config import Settings
from app.events.ids import event_id_for
from app.events.rabbitmq import connect, persistent_properties
from app.events.schemas import TRANSFER_COMPLETED
from app.repositories.outbox_repository import ClaimedEvent

QUEUE = "ledgerflow.ledger-audit"


def effect_counts(event_id: uuid.UUID) -> tuple[int, int]:
    (row,) = db_query(
        "SELECT (SELECT count(*) FROM processed_events WHERE event_id = :id), "
        "(SELECT count(*) FROM event_audit_log WHERE event_id = :id)",
        {"id": event_id},
    )
    return row[0], row[1]


def envelope_bytes(event_id: uuid.UUID) -> bytes:
    """The exact message the publisher sends for this outbox row."""
    ((row),) = db_query(
        "SELECT id, event_type, event_version, aggregate_id, payload, correlation_id, "
        "created_at, attempts FROM outbox_events WHERE id = :id",
        {"id": event_id},
    )
    return ClaimedEvent(*row).envelope().to_json()


def main() -> None:
    parser = base_arg_parser(__doc__ or "")
    parser.add_argument("--copies", type=int, default=5)
    args = parser.parse_args()
    ensure_local(args.base_url)
    wait_ready(args.base_url)

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        preflight(client)
        run_id = new_run_id()
        source = create_demo_account(client, run_id, "source")
        destination = create_demo_account(client, run_id, "destination")
        fund(client, source, 10_000)
        response = post_admitted(
            client,
            "/api/v1/transfers",
            json=transfer_body(source, destination, 2_500),
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        check(response.status_code == 201, "transfer committed")
        event_id = event_id_for(TRANSFER_COMPLETED, uuid.UUID(response.json()["id"]))
        print(f"Event {event_id}")
        wait_until(lambda: effect_counts(event_id) == (1, 1), timeout=60, what="first delivery")
        print(f"1) First delivery processed: (receipts, audit rows) = {effect_counts(event_id)}")

        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # Z: UTC (naive = local time)
        print("\n2) Producing duplicates of the SAME event:")
        reset = db_execute(  # what a lost confirm / expired lease causes: a re-publish
            "UPDATE outbox_events SET status = 'pending', available_at = now() WHERE id = :id",
            {"id": event_id},
        )
        check(reset == 1, "outbox row reset to pending")
        print("   - outbox row reset to pending -> the publisher publishes it again")
        connection = connect(Settings())  # type: ignore[call-arg]
        channel = connection.channel()
        channel.confirm_delivery()
        body = envelope_bytes(event_id)
        for _ in range(args.copies):
            channel.basic_publish(
                exchange="ledgerflow.events",
                routing_key=TRANSFER_COMPLETED,
                body=body,
                properties=persistent_properties(
                    message_id=str(event_id), event_type=TRANSFER_COMPLETED, correlation_id=None
                ),
                mandatory=True,
            )
        connection.close()
        print(f"   - {args.copies} extra copies published straight to the exchange")

        wait_until(
            lambda: (
                queue_depths().get(QUEUE, (0, 0)) == (0, 0)
                and db_query("SELECT status FROM outbox_events WHERE id = :id", {"id": event_id})[
                    0
                ][0]
                == "published"
            ),
            timeout=60,
            what="queue drained",
        )
        wait_until(
            lambda: (
                compose("logs", "--since", since, "event-consumer").stdout.count(
                    f"Duplicate delivery of event {event_id}"
                )
                >= args.copies + 1
            ),
            timeout=30,
            what="consumer to report the duplicates",
        )
        duplicates = compose("logs", "--since", since, "event-consumer").stdout.count(
            f"Duplicate delivery of event {event_id}"
        )
        print(f"\n3) Consumer reported {duplicates} duplicate deliveries and acked them")
        print(f"   (receipts, audit rows) = {effect_counts(event_id)}")
        check(duplicates == args.copies + 1, f"{args.copies + 1} duplicates were delivered")
        check(effect_counts(event_id) == (1, 1), "the durable effect still exists exactly once")
        check(queue_depths()[QUEUE] == (0, 0), "every duplicate was acknowledged")
        verify_ledger(client, [source, destination])
    print("\nPASS: at-least-once delivery, exactly-once effect.")


if __name__ == "__main__":
    main()
