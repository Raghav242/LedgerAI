"""Experiment Lab end to end inside one process: real PostgreSQL test database, real
background runner thread, real HTTP semantics (the runner's traffic goes through an
in-process ASGI transport into the same app, with X-Served-By enabled).

The multi-replica behaviour (NGINX, two replicas) is exercised by the Playwright flow
against the Compose stack; here a single "replica" answers, so replica-dependent checks
must report `skipped`, never a false pass.
"""

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.lab import experiments as lab_experiments
from app.lab.signing import LAB_TRAFFIC_HEADER
from app.main import create_app
from app.rate_limiting import RedisTokenBucketLimiter
from app.rate_limiting.redis_token_bucket import build_redis_client
from tests.integration.conftest import create_account, fund_account, get_balance, post_transfer

REPLICA = "api-test"
TERMINAL = {"passed", "failed", "error"}


def _make_lab_app(settings: Settings, **kwargs: object) -> FastAPI:
    app = create_app(settings, **kwargs)  # type: ignore[arg-type]
    # The runner's measured traffic re-enters this same app in-process.
    app.state.lab_runner.transport_factory = lambda: httpx.ASGITransport(app=app)
    return app


@pytest.fixture
def lab_settings(test_settings: Settings) -> Settings:
    return test_settings.model_copy(update={"expose_replica_header": True, "replica_id": REPLICA})


@pytest.fixture
def lab(lab_settings: Settings, engine: Engine) -> Iterator[tuple[TestClient, FastAPI]]:
    app = _make_lab_app(lab_settings)
    with TestClient(app) as client:
        yield client, app
    assert app.state.lab_runner.wait_idle(60)


def start(client: TestClient, experiment: str, params: dict | None = None) -> dict:
    response = client.post(
        "/api/v1/lab/runs", json={"experiment": experiment, "params": params or {}}
    )
    assert response.status_code == 202, response.text
    return response.json()


def wait_for(client: TestClient, run_id: str, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/v1/lab/runs/{run_id}").json()
        if run["status"] in TERMINAL:
            return run
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


def checks_by_id(run: dict) -> dict[str, dict]:
    return {c["id"]: c for c in run["checks"]}


def all_events(client: TestClient, run_id: str) -> list[dict]:
    events, after = [], 0
    while True:
        page = client.get(f"/api/v1/lab/runs/{run_id}/events", params={"after": after}).json()
        events += page["items"]
        if not page["items"]:
            return events
        after = page["next_after"]


# --- catalog, gating, validation ------------------------------------------------------------


def test_catalog_lists_the_allow_listed_experiments(lab: tuple[TestClient, FastAPI]) -> None:
    client, _ = lab
    response = client.get("/api/v1/lab/experiments")
    assert response.status_code == 200
    catalog = response.json()
    assert [e["id"] for e in catalog] == [
        "concurrency",
        "idempotency",
        "rate_limiting",
        "load_balancing",
    ]
    assert all(e["risk"] == "safe_local" and e["params"] for e in catalog)


def test_lab_does_not_exist_without_dev_endpoints(test_settings: Settings, engine: Engine) -> None:
    app = create_app(test_settings.model_copy(update={"enable_dev_endpoints": False}))
    with TestClient(app) as client:
        assert client.get("/api/v1/lab/experiments").status_code == 404
        assert client.post("/api/v1/lab/runs", json={"experiment": "concurrency"}).status_code in (
            404,
            405,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"experiment": "shell", "params": {"cmd": "id"}},
        {"experiment": "concurrency", "params": {"requests": 100_000}},
        {"experiment": "concurrency", "params": {"base_url": "http://example.com"}},
        {"experiment": "load_balancing", "target": "http://example.com"},
        {"experiment": "rate_limiting", "params": {"refill_wait_seconds": -1}},
    ],
)
def test_unsafe_or_unknown_requests_are_rejected_and_nothing_runs(
    lab: tuple[TestClient, FastAPI], payload: dict
) -> None:
    client, app = lab
    response = client.post("/api/v1/lab/runs", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get("/api/v1/lab/runs").json() == []
    assert not app.state.lab_runner.busy


def test_unknown_run_is_404(lab: tuple[TestClient, FastAPI]) -> None:
    client, _ = lab
    response = client.get(f"/api/v1/lab/runs/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "LAB_RUN_NOT_FOUND"


# --- the four experiments --------------------------------------------------------------------


def test_concurrency_experiment(lab: tuple[TestClient, FastAPI]) -> None:
    client, _ = lab
    bystander = create_account(client, "Bystander")
    fund_account(client, bystander["id"], 5_000)

    run = wait_for(
        client,
        start(client, "concurrency", {"requests": 30, "amount_minor": 1_000})["id"],
    )

    assert run["status"] == "passed", run
    assert run["params"] == {"requests": 30, "amount_minor": 1_000, "funding_minor": 10_000}
    assert run["result"]["outcome_counts"] == {"committed": 10, "insufficient_funds": 20}
    assert run["result"]["data"]["final_balances"] == {"source": 0, "destination": 10_000}
    checks = checks_by_id(run)
    assert {c["status"] for c in checks.values()} == {"pass"}
    assert {"exact_successes", "no_overdraft", "money_conserved", "reconciliation"} <= set(checks)
    assert run["runner_replica"] == REPLICA
    assert run["started_at"] and run["finished_at"]
    assert run["progress"]["phase"] == "done"

    # Isolation: the run touched only its own, uniquely named accounts.
    assert get_balance(client, bystander["id"]) == 5_000
    lab_accounts = [
        a
        for a in client.get("/api/v1/accounts", params={"limit": 100}).json()["items"]
        if a["name"].startswith(f"lab-{run['id'].replace('-', '')[:8]}-")
    ]
    assert {a["name"].rsplit("-", 1)[1] for a in lab_accounts} == {"source", "destination"}


def test_idempotency_experiment(lab: tuple[TestClient, FastAPI]) -> None:
    client, _ = lab
    run = wait_for(client, start(client, "idempotency", {"duplicates": 12})["id"])

    assert run["status"] == "passed", run
    counts = run["result"]["outcome_counts"]
    assert counts == {"committed": 1, "replayed": 11}
    data = run["result"]["data"]
    assert len(data["transfer_ids"]) == 1
    assert data["reuse_probe"] == {"status": 422, "code": "IDEMPOTENCY_KEY_REUSED"}
    assert data["balances_after_duplicates"]["destination"] == 1_000
    assert checks_by_id(run)["one_recorded"]["status"] == "pass"


def test_load_balancing_against_a_single_replica_skips_replica_checks(
    lab: tuple[TestClient, FastAPI],
) -> None:
    client, _ = lab
    run = wait_for(client, start(client, "load_balancing", {"requests": 8})["id"])

    assert run["status"] == "passed", run
    assert run["result"]["data"]["counts"] == {REPLICA: 8}
    assert run["result"]["data"]["sequence"] == [REPLICA] * 8
    checks = checks_by_id(run)
    assert checks["all_succeeded"]["status"] == "pass"
    assert checks["replica_identified"]["status"] == "pass"
    # One process cannot demonstrate load balancing: reported as skipped, not passed.
    assert checks["multiple_replicas"]["status"] == "skipped"
    assert checks["even_split"]["status"] == "skipped"


@pytest.mark.redis
def test_rate_limiting_experiment_with_the_real_redis_token_bucket(
    lab_settings: Settings, engine: Engine
) -> None:
    redis_client = build_redis_client(
        "redis://127.0.0.1:6379/15", connect_timeout_s=1, socket_timeout_s=1, max_connections=20
    )
    try:
        redis_client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis not reachable: {exc}")
    prefix = f"test:{uuid.uuid4().hex}"
    settings = lab_settings.model_copy(
        update={
            "rate_limit_backend": "redis",
            "rate_limit_write_capacity": 5,
            "rate_limit_write_refill_tokens": 5,
            "rate_limit_write_refill_interval_ms": 1000,
        }
    )
    app = _make_lab_app(
        settings, rate_limiter=RedisTokenBucketLimiter(redis_client, key_prefix=prefix)
    )
    try:
        with TestClient(app) as client:
            params = {"burst_requests": 15, "refill_wait_seconds": 1, "probe_requests": 10}
            run = wait_for(client, start(client, "rate_limiting", params)["id"])
            assert app.state.lab_runner.wait_idle(30)
    finally:
        keys = list(redis_client.scan_iter(f"{prefix}:*"))
        if keys:
            redis_client.delete(*keys)
        redis_client.close()

    assert run["status"] == "passed", run
    burst = run["result"]["data"]["burst"]
    assert 5 <= burst["allowed"] <= burst["allowed_range"][1]
    assert burst["allowed"] + burst["rejected"] == 15
    assert run["result"]["data"]["policy"]["capacity"] == 5
    assert run["result"]["data"]["retry_after_values"]  # every 429 carried Retry-After
    checks = checks_by_id(run)
    for check_id in (
        "global_quota",
        "excess_rejected",
        "retry_after",
        "money_only_for_admitted",
        "refill",
        "redis_backend",
    ):
        assert checks[check_id]["status"] == "pass", checks[check_id]
    assert checks["shared_across_replicas"]["status"] == "skipped"  # one process only


# --- lifecycle, events, overlap, failure ------------------------------------------------------


def test_events_form_an_incremental_timeline(lab: tuple[TestClient, FastAPI]) -> None:
    client, _ = lab
    run = wait_for(client, start(client, "load_balancing", {"requests": 5})["id"])

    first = client.get(f"/api/v1/lab/runs/{run['id']}/events", params={"limit": 3}).json()
    assert [e["seq"] for e in first["items"]] == [1, 2, 3]
    assert first["next_after"] == 3
    events = all_events(client, run["id"])
    assert [e["seq"] for e in events] == list(range(1, run["event_count"] + 1))
    kinds = {e["kind"] for e in events}
    assert kinds == {"phase", "request", "check", "note"}
    requests = [e["request"] for e in events if e["kind"] == "request"]
    assert len(requests) == 5
    assert all(r["replica"] == REPLICA and r["outcome"] == "read_ok" for r in requests)
    assert all(r["rate_policy"] == "lab-burst-read" for r in requests)


def test_only_one_run_can_be_active(lab: tuple[TestClient, FastAPI], engine: Engine) -> None:
    client, _ = lab
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO lab_runs (id, experiment, status, params, progress, runner_replica, "
                "target_base_url) VALUES (:id, 'concurrency', 'running', '{}', '{}', 'api-x', "
                "'http://x')"
            ),
            {"id": uuid.uuid4()},
        )
    response = client.post("/api/v1/lab/runs", json={"experiment": "load_balancing"})
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "LAB_RUN_IN_PROGRESS"
    assert body["details"]["active_experiment"] == "concurrency"


def test_a_run_whose_runner_died_is_expired(
    lab: tuple[TestClient, FastAPI], engine: Engine
) -> None:
    client, _ = lab
    stale_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO lab_runs (id, experiment, status, params, progress, runner_replica, "
                "target_base_url, heartbeat_at) VALUES (:id, 'concurrency', 'running', '{}', "
                "'{}', 'api-x', 'http://x', :hb)"
            ),
            {"id": stale_id, "hb": datetime.now(UTC) - timedelta(minutes=5)},
        )
    run = wait_for(client, start(client, "load_balancing", {"requests": 2})["id"])
    assert run["status"] == "passed"
    stale = client.get(f"/api/v1/lab/runs/{stale_id}").json()
    assert stale["status"] == "error"
    assert "stopped responding" in stale["error"]


def test_an_unreachable_target_fails_the_invariants_instead_of_hanging(
    lab_settings: Settings, engine: Engine
) -> None:
    settings = lab_settings.model_copy(update={"lab_target_base_url": "http://127.0.0.1:9"})
    app = create_app(settings)  # real network transport to a closed port
    with TestClient(app) as client:
        run = wait_for(client, start(client, "load_balancing", {"requests": 2})["id"])
        assert app.state.lab_runner.wait_idle(30)
    assert run["status"] == "failed"
    assert checks_by_id(run)["all_succeeded"]["status"] == "fail"
    assert run["result"]["outcome_counts"] == {"transport_error": 2}


def test_an_experiment_crash_is_recorded_as_error(
    lab: tuple[TestClient, FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = lab

    async def boom(ctx: object, params: object) -> None:
        raise RuntimeError("simulated experiment bug")

    monkeypatch.setitem(lab_experiments.EXPERIMENTS, "load_balancing", boom)
    run = wait_for(client, start(client, "load_balancing")["id"])
    assert run["status"] == "error"
    assert "simulated experiment bug" in run["error"]
    assert run["result"] is None
    assert run["finished_at"] is not None
    # A finished run frees the slot.
    monkeypatch.undo()
    assert wait_for(client, start(client, "load_balancing", {"requests": 2})["id"])["status"] == (
        "passed"
    )


def test_recent_runs_are_listed_newest_first(lab: tuple[TestClient, FastAPI]) -> None:
    client, _ = lab
    first = wait_for(client, start(client, "load_balancing", {"requests": 2})["id"])
    second = wait_for(client, start(client, "load_balancing", {"requests": 3})["id"])
    runs = client.get("/api/v1/lab/runs").json()
    assert [r["id"] for r in runs] == [second["id"], first["id"]]
    assert runs[0]["checks_total"] == 2  # replica checks skipped, not counted
    assert runs[0]["checks_passed"] == 2
    assert runs[0]["summary"]


# --- the lab token cannot be forged by a client ------------------------------------------------


def test_a_forged_lab_header_does_not_change_the_callers_scope(
    lab: tuple[TestClient, FastAPI],
) -> None:
    client, _ = lab
    source = create_account(client, "Forger")["id"]
    destination = create_account(client, "Target")["id"]
    fund_account(client, source, 1_000)
    key = str(uuid.uuid4())
    forged = f"v1.{uuid.uuid4().hex}.burst.{'a' * 64}"
    body = {
        "source_account_id": source,
        "destination_account_id": destination,
        "amount_minor": 100,
        "currency": "USD",
    }
    first = client.post(
        "/api/v1/transfers",
        json=body,
        headers={"Idempotency-Key": key, LAB_TRAFFIC_HEADER: forged},
    )
    assert first.status_code == 201
    assert first.headers["X-RateLimit-Policy"] == "write"  # not the lab burst policy
    # Same key WITHOUT the header replays: both requests were in the demo-merchant scope.
    second = post_transfer(client, source, destination, 100, idempotency_key=key)
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert get_balance(client, source) == 900
