"""Phase 5C observability against the real PostgreSQL test database.

- the topology reports workers from their heartbeats and never shows a stale one as current
- event pipeline numbers and lifecycles reflect exactly the persisted state
- every observability route is a GET (read-only)
"""

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.events.ids import event_id_for
from app.events.schemas import ACCOUNT_FUNDED, TRANSFER_COMPLETED
from app.observability.heartbeats import HeartbeatReporter
from tests.integration.conftest import create_account, fund_account, post_transfer

Factory = sessionmaker[Session]


def _components(client: TestClient, **headers: str) -> dict[str, dict]:
    response = client.get("/api/v1/observability/topology", headers=headers)
    assert response.status_code == 200, response.text
    return {c["id"]: c for c in response.json()["components"]}


def _transfer(client: TestClient, amount: int = 700) -> tuple[str, str]:
    a = create_account(client, "Obs A")["id"]
    b = create_account(client, "Obs B")["id"]
    fund_account(client, a, 10_000)
    response = post_transfer(client, a, b, amount)
    assert response.status_code == 201, response.text
    return a, response.json()["id"]


def _reporter(factory: Factory, component: str, interval: float = 5.0) -> HeartbeatReporter:
    return HeartbeatReporter(
        factory, component=component, instance=component, interval_seconds=interval
    )


# --- topology ---------------------------------------------------------------------------------


def test_nginx_status_depends_on_the_request_actually_passing_through_it(
    client: TestClient,
) -> None:
    assert _components(client)["nginx"]["status"] == "unknown"
    via_nginx = _components(client, **{"X-Request-ID": "abc123"})["nginx"]
    assert (via_nginx["status"], via_nginx["evidence"]) == ("healthy", "request")


def test_database_is_probed_and_single_process_reports_itself(client: TestClient) -> None:
    components = _components(client)
    assert components["postgres"]["status"] == "healthy"
    assert components["postgres"]["latency_ms"] is not None
    api = [c for c in components.values() if c["kind"] == "api"]
    assert len(api) == 1 and api[0]["status"] == "healthy"
    # The test app uses the in-memory limiter: Redis is not part of this deployment.
    assert components["redis"]["status"] == "not_configured"
    assert components["jaeger"]["status"] == "not_configured"


def test_workers_without_heartbeats_are_unknown_never_healthy(client: TestClient) -> None:
    components = _components(client)
    assert components["outbox-publisher"]["status"] == "unknown"
    assert components["event-consumer"]["status"] == "unknown"
    assert components["anomaly-detector"]["status"] == "not_configured"
    assert components["rabbitmq"]["status"] == "unknown"


def test_fresh_heartbeats_are_healthy_and_the_broker_is_derived_from_them(
    client: TestClient, session_factory: Factory
) -> None:
    publisher = _reporter(session_factory, "outbox-publisher")
    assert publisher.beat("ok", {"broker_connected": True, "metrics": []})
    consumer = _reporter(session_factory, "event-consumer")
    queues = [{"queue": "ledgerflow.ledger-audit", "role": "main", "ready": 3, "consumers": 1}]
    assert consumer.beat(
        "ok", {"broker_connected": True, "consumer": "ledger-audit", "queues": queues}
    )
    components = _components(client)
    assert components["outbox-publisher"]["status"] == "healthy"
    assert components["outbox-publisher"]["evidence"] == "heartbeat"
    assert components["outbox-publisher"]["last_ok_at"] is not None
    broker = components["rabbitmq"]
    assert (broker["status"], broker["evidence"]) == ("healthy", "derived")
    assert any(m["value"] == 3 for m in broker["metrics"])


def test_degraded_report_shows_the_broker_down_with_the_error(
    client: TestClient, session_factory: Factory
) -> None:
    consumer = _reporter(session_factory, "event-consumer")
    consumer.record_error("broker unavailable: AMQPConnectionError")
    consumer.beat("degraded", {"broker_connected": False, "consumer": "ledger-audit"}, force=True)
    components = _components(client)
    assert components["event-consumer"]["status"] == "degraded"
    broker = components["rabbitmq"]
    assert broker["status"] == "unhealthy"
    assert "AMQPConnectionError" in broker["last_error"]


def test_heartbeat_past_its_deadline_is_stale_not_current(
    client: TestClient, session_factory: Factory, engine: Engine
) -> None:
    _reporter(session_factory, "outbox-publisher").beat("ok", {"broker_connected": True})
    with engine.begin() as conn:  # simulate a worker that stopped 2 minutes ago
        conn.execute(
            text(
                "UPDATE worker_heartbeats SET beat_at = now() - interval '2 minutes', "
                "next_beat_before = now() - interval '100 seconds'"
            )
        )
    components = _components(client)
    assert components["outbox-publisher"]["status"] == "stale"
    assert components["outbox-publisher"]["age_seconds"] >= 119
    # A stale report says nothing about the broker either.
    assert components["rabbitmq"]["status"] == "unknown"


def test_announced_back_off_extends_the_deadline(session_factory: Factory, engine: Engine) -> None:
    reporter = _reporter(session_factory, "event-consumer", interval=1)
    reporter.beat("degraded", {}, force=True, next_within_seconds=60)
    with engine.connect() as conn:
        remaining = conn.scalar(
            text("SELECT extract(epoch FROM next_beat_before - now()) FROM worker_heartbeats")
        )
    assert 60 < remaining <= 64  # 3 intervals of grace + the announced 60 s


def test_heartbeats_are_throttled(session_factory: Factory) -> None:
    reporter = _reporter(session_factory, "outbox-publisher", interval=60)
    assert reporter.beat("ok", {})
    assert not reporter.beat("ok", {})  # not due yet
    assert reporter.beat("degraded", {}, force=True)  # problems are reported at once


# --- event pipeline ---------------------------------------------------------------------------


def test_pipeline_summary_and_lifecycle_follow_persisted_state(
    client: TestClient, engine: Engine
) -> None:
    _, transfer_id = _transfer(client)
    event_id = event_id_for(TRANSFER_COMPLETED, uuid.UUID(transfer_id))

    summary = client.get("/api/v1/observability/events/summary").json()
    assert (summary["outbox_pending"], summary["outbox_published"]) == (2, 0)  # funding + transfer
    assert summary["oldest_unpublished_age_seconds"] is not None

    life = client.get(f"/api/v1/observability/transfers/{transfer_id}/lifecycle").json()
    stages = {s["id"]: s for s in life["stages"]}
    assert life["event"]["id"] == str(event_id)
    assert stages["commit"]["state"] == "observed"
    assert stages["outbox"]["state"] == "observed"
    assert stages["publish"]["state"] == "pending"
    assert stages["consume:ledger-audit"]["state"] == "pending"

    # A failed attempt is reported as such, with the error, while the event stays safe.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox_events SET attempts = 1, last_error = 'broker down' WHERE id = :id"
            ),
            {"id": event_id},
        )
    stages = {
        s["id"]: s for s in client.get(f"/api/v1/observability/events/{event_id}").json()["stages"]
    }
    assert stages["publish"]["state"] == "failed" and "broker down" in stages["publish"]["detail"]
    assert client.get("/api/v1/observability/events/summary").json()["awaiting_retry"] == 1

    # Published + consumed: observed, with measured durations from the stored timestamps.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox_events SET status = 'published', attempts = 2, last_error = NULL, "
                "published_at = created_at + interval '250 milliseconds' WHERE id = :id"
            ),
            {"id": event_id},
        )
        conn.execute(
            text(
                "INSERT INTO processed_events (event_id, consumer_name, processed_at) "
                "SELECT id, 'ledger-audit', published_at + interval '40 milliseconds' "
                "FROM outbox_events WHERE id = :id"
            ),
            {"id": event_id},
        )
    life = client.get(f"/api/v1/observability/transfers/{transfer_id}/lifecycle").json()
    stages = {s["id"]: s for s in life["stages"]}
    assert stages["publish"]["state"] == "observed"
    assert stages["publish"]["duration_ms"] == 250.0
    assert stages["consume:ledger-audit"]["state"] == "observed"
    assert stages["consume:ledger-audit"]["duration_ms"] == 40.0
    summary = client.get("/api/v1/observability/events/summary").json()
    assert summary["republished_events"] == 1
    audit = next(c for c in summary["consumers"] if c["consumer"] == "ledger-audit")
    assert audit["events_processed"] == 1


def test_events_are_paginated_newest_first_and_filterable(client: TestClient) -> None:
    for _ in range(3):
        _transfer(client)  # 2 events each
    first = client.get("/api/v1/observability/events", params={"limit": 4}).json()
    assert len(first["items"]) == 4 and first["next_cursor"]
    second = client.get(
        "/api/v1/observability/events", params={"limit": 4, "cursor": first["next_cursor"]}
    ).json()
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    ids = [e["id"] for e in first["items"] + second["items"]]
    assert len(set(ids)) == 6
    stamps = [e["created_at"] for e in first["items"] + second["items"]]
    assert stamps == sorted(stamps, reverse=True)
    funded = client.get(
        "/api/v1/observability/events", params={"event_type": ACCOUNT_FUNDED}
    ).json()["items"]
    assert len(funded) == 3 and all(e["event_type"] == ACCOUNT_FUNDED for e in funded)
    assert "payload" not in funded[0]  # only the amount and transfer id are exposed
    assert client.get("/api/v1/observability/events", params={"limit": 101}).status_code == 422
    bad = client.get("/api/v1/observability/events", params={"cursor": "nope"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_CURSOR"


def test_unknown_ids_are_404(client: TestClient) -> None:
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/observability/events/{missing}").status_code == 404
    response = client.get(f"/api/v1/observability/transfers/{missing}/lifecycle")
    assert response.status_code == 404


def test_trace_endpoint_without_tracing_says_so(client: TestClient) -> None:
    response = client.get(f"/api/v1/observability/traces/{'a' * 32}")
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert client.get("/api/v1/observability/traces/not-a-trace").status_code == 422


def test_observability_routes_are_read_only(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    ours = {p: ops for p, ops in paths.items() if p.startswith("/api/v1/observability")}
    assert len(ours) >= 6
    assert all(set(ops) == {"get"} for ops in ours.values()), ours


def test_observability_is_not_mounted_without_dev_endpoints(test_settings, engine) -> None:  # type: ignore[no-untyped-def]
    from app.main import create_app

    app = create_app(test_settings.model_copy(update={"enable_dev_endpoints": False}))
    with TestClient(app) as plain:
        assert plain.get("/api/v1/observability/topology").status_code == 404


@pytest.mark.parametrize("_", range(1))
def test_snapshot_is_bounded_in_time(client: TestClient, _: int) -> None:
    started = time.monotonic()
    _components(client)
    assert time.monotonic() - started < 5


def test_consumer_before_recorded_publish_has_no_negative_duration(
    client: TestClient, engine: Engine
) -> None:
    _, transfer_id = _transfer(client)
    event_id = event_id_for(TRANSFER_COMPLETED, uuid.UUID(transfer_id))
    with engine.begin() as conn:  # published_at is recorded after the confirm, consumer was faster
        conn.execute(
            text(
                "UPDATE outbox_events SET status = 'published', attempts = 1, "
                "published_at = created_at + interval '300 milliseconds' WHERE id = :id"
            ),
            {"id": event_id},
        )
        conn.execute(
            text(
                "INSERT INTO processed_events (event_id, consumer_name, processed_at) "
                "SELECT id, 'ledger-audit', published_at - interval '3 milliseconds' "
                "FROM outbox_events WHERE id = :id"
            ),
            {"id": event_id},
        )
    life = client.get(f"/api/v1/observability/transfers/{transfer_id}/lifecycle").json()
    stage = next(s for s in life["stages"] if s["id"] == "consume:ledger-audit")
    assert stage["state"] == "observed" and stage["duration_ms"] is None
    assert "before the publisher recorded" in stage["detail"]
