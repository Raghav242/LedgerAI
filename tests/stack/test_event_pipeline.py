"""End-to-end through the real containers: NGINX -> API -> outbox -> publisher ->
RabbitMQ -> consumer -> audit row, in the stack-test database."""

import uuid

import httpx
from sqlalchemy import text

from app.events.ids import event_id_for
from app.events.schemas import TRANSFER_COMPLETED
from tests.stack.conftest import Stack


def test_transfers_through_nginx_are_delivered_once_each(stack: Stack, fresh_quota: None) -> None:
    with httpx.Client(base_url=stack.base_url, timeout=30) as http:
        a = http.post("/api/v1/accounts", json={"name": "evt-a"}).json()["id"]
        b = http.post("/api/v1/accounts", json={"name": "evt-b"}).json()["id"]
        http.post(f"/api/v1/accounts/{a}/fund", json={"amount_minor": 10_000}).raise_for_status()
        transfer_ids = []
        for _ in range(5):
            key = str(uuid.uuid4())
            body = {
                "source_account_id": a,
                "destination_account_id": b,
                "amount_minor": 10,
                "currency": "USD",
            }
            first = http.post("/api/v1/transfers", json=body, headers={"Idempotency-Key": key})
            replay = http.post("/api/v1/transfers", json=body, headers={"Idempotency-Key": key})
            assert first.status_code == replay.status_code == 201
            transfer_ids.append(uuid.UUID(first.json()["id"]))

    event_ids = [event_id_for(TRANSFER_COMPLETED, t) for t in transfer_ids]

    from stack_control import wait_until

    def audited() -> int:
        with stack.db.connect() as conn:
            return conn.execute(
                text("SELECT count(*) FROM event_audit_log WHERE event_id = ANY(:ids)"),
                {"ids": event_ids},
            ).scalar_one()

    wait_until(lambda: audited() == 5, timeout=60, interval=0.2, what="5 audited events")
    with stack.db.connect() as conn:
        outbox = conn.execute(
            text("SELECT status, correlation_id FROM outbox_events WHERE id = ANY(:ids)"),
            {"ids": event_ids},
        ).all()
        receipts = conn.execute(
            text(
                "SELECT count(*) FROM processed_events "
                "WHERE event_id = ANY(:ids) AND consumer_name = 'ledger-audit'"
            ),
            {"ids": event_ids},
        ).scalar_one()

    # The AI worker is a second, independent consumer: one receipt and one result per event.
    def ai_scored() -> tuple[int, int]:
        with stack.db.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM processed_events WHERE event_id = ANY(:ids) "
                    "        AND consumer_name = 'anomaly-detector'), "
                    "(SELECT count(*) FROM anomaly_results WHERE event_id = ANY(:ids))"
                ),
                {"ids": event_ids},
            ).one()
            return row[0], row[1]

    wait_until(lambda: ai_scored() == (5, 5), timeout=60, interval=0.2, what="5 AI results")
    assert len(outbox) == 5  # replays added no events
    assert {status for status, _ in outbox} == {"published"}
    assert all(correlation for _, correlation in outbox)  # NGINX request ids carried along
    assert receipts == 5 and audited() == 5
