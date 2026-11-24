"""Phase 6A: restart behaviour and runtime least privilege of the real Compose stack.

Restarts containers (never removes them or their volumes), so committed data must survive
and readiness must come back without manual steps. Runs in the isolated stack-test
project like every stack test; the development stack is never touched.
"""

import json
import subprocess
import uuid

import httpx
import stack_control

from tests.stack.conftest import Stack


def _wait_ready(base_url: str, timeout: float = 90) -> None:
    def ready() -> bool:
        try:
            return httpx.get(f"{base_url}/health/ready", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    stack_control.wait_until(ready, timeout=timeout, interval=1, what="readiness through NGINX")


def _reconciled(http: httpx.Client) -> dict:
    report = http.get("/api/v1/reconciliation").raise_for_status().json()
    assert report["ok"] is True, report
    assert report["ledger_total_minor"] == 0
    return report


def _transfer(http: httpx.Client, a: str, b: str, key: str) -> httpx.Response:
    body = {
        "source_account_id": a,
        "destination_account_id": b,
        "amount_minor": 25,
        "currency": "USD",
    }
    return http.post("/api/v1/transfers", json=body, headers={"Idempotency-Key": key})


def test_restarts_preserve_committed_money_and_idempotency(stack: Stack, fresh_quota: None) -> None:
    with httpx.Client(base_url=stack.base_url, timeout=30) as http:
        a = http.post("/api/v1/accounts", json={"name": "restart-a"}).json()["id"]
        b = http.post("/api/v1/accounts", json={"name": "restart-b"}).json()["id"]
        http.post(f"/api/v1/accounts/{a}/fund", json={"amount_minor": 1_000}).raise_for_status()
        key = str(uuid.uuid4())
        first = _transfer(http, a, b, key)
        assert first.status_code == 201
        transfer_id = first.json()["id"]

    # PostgreSQL restart: data lives in the named volume; pools reconnect on their own.
    stack.compose("restart", "postgres")
    _wait_ready(stack.base_url)
    # A replica and Redis restart (rate-limit state is ephemeral by design).
    stack.compose("restart", "api-1", "redis")
    _wait_ready(stack.base_url)
    stack_control.wait_until_served_only_by(stack.base_url, {"api-1", "api-2"}, timeout=60)
    stack_control.reset_rate_limits(stack.project.redis_url)

    with httpx.Client(base_url=stack.base_url, timeout=30) as http:
        assert http.get(f"/api/v1/transfers/{transfer_id}").status_code == 200
        # The durable idempotency record survived: the retry replays, it does not re-execute.
        replay = _transfer(http, a, b, key)
        assert replay.status_code == 201
        assert replay.headers.get("Idempotent-Replayed") == "true"
        assert replay.json()["id"] == transfer_id
        balances = {
            acc: http.get(f"/api/v1/accounts/{acc}").json()["balance_minor"] for acc in (a, b)
        }
        assert balances == {a: 975, b: 25}
        _reconciled(http)


def _env(container: str) -> dict[str, str]:
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{json .Config.Env}}", container],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(item.split("=", 1) for item in json.loads(out))


def test_runtime_secrets_follow_least_privilege(stack: Stack) -> None:
    api = _env(stack.container("api-1"))
    assert "RABBITMQ_PASSWORD" not in api  # the API never holds broker admin credentials
    # Phase 6B: replicas connect as the least-privilege runtime role, never the owner.
    assert api["POSTGRES_USER"] == "ledgerflow_app"
    assert "APP_DB_PASSWORD" not in api
    assert api["LAB_TRAFFIC_SECRET"]  # required: dev endpoints are on in the stack tests
    assert api["LAB_TRAFFIC_SECRET"] != api["POSTGRES_PASSWORD"]
    assert api["FORWARDED_ALLOW_IPS"].endswith(".12")  # only NGINX may set X-Forwarded-*
    assert api["GATEWAY_BIND"] == "127.0.0.1"
    for worker in ("outbox-publisher", "event-consumer", "ai-worker"):
        env = _env(stack.container(worker))
        assert env["POSTGRES_USER"] == "ledgerflow_app"
        assert env["LAB_TRAFFIC_SECRET"] == ""
        assert env["RABBITMQ_MONITOR_PASSWORD"] == ""
        assert env["ENABLE_DEV_ENDPOINTS"] == "false"


def test_broker_health_comes_from_the_independent_probe(stack: Stack) -> None:
    with httpx.Client(base_url=stack.base_url, timeout=30) as http:
        snapshot = http.get("/api/v1/observability/topology").raise_for_status().json()
    broker = next(c for c in snapshot["components"] if c["id"] == "rabbitmq")
    assert broker["evidence"] == "probe", broker
    assert broker["status"] == "healthy", broker
    keys = {m["key"] for m in broker["metrics"]}
    assert "dead_lettered" in keys and "workers_connected" in keys


def test_each_consumer_dead_letters_only_into_its_own_dlq(stack: Stack) -> None:
    """Phase 6B: the consumers applied their routing policies on the stack's broker and the
    shared fanout no longer feeds the DLQs directly."""
    import os

    from app.core.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    assert settings.rabbitmq_password is not None
    port = stack.project.env["RABBITMQ_MANAGEMENT_PORT"]
    auth = (settings.rabbitmq_user, settings.rabbitmq_password.get_secret_value())
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", auth=auth, timeout=10) as admin:

        def policies() -> dict:
            return {p["name"]: p for p in admin.get("/api/policies/%2F").json()}

        stack_control.wait_until(
            lambda: (
                {"ledgerflow-dead-letter-ledger-audit", "ledgerflow-dead-letter-anomaly-detector"}
                <= set(policies())
            ),
            timeout=60,
            what="dead-letter policies",
        )
        audit = policies()["ledgerflow-dead-letter-ledger-audit"]["definition"]
        assert audit == {"dead-letter-routing-key": "ledgerflow.ledger-audit.dlq"}
        bindings = admin.get("/api/exchanges/%2F/ledgerflow.events.dlx/bindings/source").json()
    destinations = {(b["destination"], b["destination_type"]) for b in bindings}
    assert destinations == {("ledgerflow.events.dlx.by-queue", "exchange")}
    assert os.environ.get("COMPOSE_PROJECT_NAME") != "ledgerflow"


def test_backup_restores_into_a_throwaway_server_and_reconciles(stack: Stack) -> None:
    """Phase 6B: snapshot-consistent backup of the live stack-test database (workers
    running), restored into a throwaway PostgreSQL container and verified there."""
    import db_backup

    with httpx.Client(base_url=stack.base_url, timeout=30) as http:
        a = http.post("/api/v1/accounts", json={"name": "backup-a"}).json()["id"]
        b = http.post("/api/v1/accounts", json={"name": "backup-b"}).json()["id"]
        http.post(f"/api/v1/accounts/{a}/fund", json={"amount_minor": 500}).raise_for_status()
        assert _transfer(http, a, b, str(uuid.uuid4())).status_code == 201
    path = db_backup.backup(stack.project)
    try:
        report = db_backup.verify(path)
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
    assert report["ok"] and report["row_counts_match"]
    assert report["ledger_total_minor"] == 0 and report["mismatched_accounts"] == 0
