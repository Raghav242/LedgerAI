"""Worker crash demo: the consumer process dies mid-event, twice, and nothing breaks.

  A) crash AFTER the database commit, BEFORE the ack -> broker redelivers -> duplicate
     detected via processed_events -> effect stays exactly once.
  B) crash BEFORE the database commit -> transaction rolled back -> redelivered ->
     processed for real on the second attempt.

The crash is real (os._exit inside the consumer container, DEV-ONLY fault injection keyed
to this demo's event ids); Docker's restart policy brings the consumer back.
LOCAL ONLY. The consumer is always restored without fault injection at exit.

    python scripts/demo_worker_crash.py
"""

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
    container_restart_count,
    db_query,
    ensure_local,
    queue_depths,
    wait_ready,
    wait_until,
)

from app.events.ids import event_id_for
from app.events.schemas import TRANSFER_COMPLETED

CONSUMER = "event-consumer"
CONTAINER = "ledgerflow-event-consumer-1"
QUEUE = "ledgerflow.ledger-audit"


def effect_counts(event_id: uuid.UUID) -> tuple[int, int]:
    (row,) = db_query(
        "SELECT (SELECT count(*) FROM processed_events WHERE event_id = :id), "
        "(SELECT count(*) FROM event_audit_log WHERE event_id = :id)",
        {"id": event_id},
    )
    return row[0], row[1]


def published(event_id: uuid.UUID) -> bool:
    rows = db_query("SELECT status FROM outbox_events WHERE id = :id", {"id": event_id})
    return bool(rows) and rows[0][0] == "published"


def start_consumer(env: dict[str, str]) -> None:
    # --build: never run a stale image (code changes since the last build would be missing).
    # --no-deps: recreate only the consumer. No --wait: it is about to crash on purpose.
    compose("up", "-d", "--build", "--no-deps", CONSUMER, env=env)


def run_scenario(client: httpx.Client, source: str, destination: str, *, when: str) -> None:
    variable = f"CONSUMER_CRASH_{when.upper()}_COMMIT_EVENT"
    print(f"\n=== Scenario: crash {when} the database commit ===")
    compose("stop", CONSUMER)
    response = post_admitted(
        client,
        "/api/v1/transfers",
        json=transfer_body(source, destination, 100),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    check(response.status_code == 201, "transfer committed")
    event_id = event_id_for(TRANSFER_COMPLETED, uuid.UUID(response.json()["id"]))
    wait_until(lambda: published(event_id), timeout=60, what="event published")
    print(f"   event {event_id} published and waiting in the queue (consumer stopped)")

    start_consumer({variable: str(event_id)})
    restarts_before = container_restart_count(CONTAINER)
    print(f"   consumer started with {variable}={event_id}")
    duplicate_line = f"Duplicate delivery of event {event_id}"

    def settled() -> bool:
        # Done = effect committed AND the message acked (queue fully drained). After a
        # crash-after-commit, also wait for the restarted consumer's duplicate handling.
        if effect_counts(event_id) != (1, 1) or queue_depths().get(QUEUE, (0, 0)) != (0, 0):
            return False
        return when != "after" or duplicate_line in compose("logs", CONSUMER).stdout

    wait_until(settled, timeout=120, what="crash, restart, redelivery and ack")
    logs = compose("logs", CONSUMER).stdout
    crashed = f"FAULT INJECTION: crashing {when} commit for event {event_id}" in logs
    duplicate = duplicate_line in logs
    print(
        f"   crash observed: {crashed}; container restarts: "
        f"{container_restart_count(CONTAINER) - restarts_before}"
    )
    print(f"   (receipts, audit rows) = {effect_counts(event_id)}")
    check(crashed, "the consumer process really died mid-event")
    check(effect_counts(event_id) == (1, 1), "effect applied exactly once")
    check(queue_depths()[QUEUE] == (0, 0), "message finally acknowledged (queue drained)")
    if when == "after":
        check(duplicate, "redelivery after the crash was recognised as a duplicate and acked")
    else:
        check(not duplicate, "the rolled-back attempt left no receipt; redelivery processed it")
    start_consumer({})  # clear the fault injection again


def main() -> None:
    args = base_arg_parser(__doc__ or "").parse_args()
    ensure_local(args.base_url)
    wait_ready(args.base_url)
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        preflight(client)
        run_id = new_run_id()
        source = create_demo_account(client, run_id, "source")
        destination = create_demo_account(client, run_id, "destination")
        fund(client, source, 10_000)
        try:
            run_scenario(client, source, destination, when="after")
            run_scenario(client, source, destination, when="before")
        finally:
            print("\nRestoring the consumer without fault injection ...")
            start_consumer({})
        verify_ledger(client, [source, destination])
    print("\nPASS: consumer crashes before or after commit never duplicate or lose an effect.")


if __name__ == "__main__":
    main()
