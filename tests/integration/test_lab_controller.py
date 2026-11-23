"""The local experiment controller's safety properties, with a FAKE Docker runner.

Nothing here stops a real container: every docker invocation goes to a recorder. What is
tested is the controller itself: access control, the allow-list, parameter validation,
exclusivity, timeouts, guaranteed restoration, honest failure reporting and recovery of an
interrupted run (lab runs live in the PostgreSQL test database).
"""

import asyncio
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.lab.context import ExperimentContext, ExperimentOutcome, check
from labctl.experiments import Env
from labctl.infra import Infra, InfraNotAllowedError, Journal
from labctl.server import TOKEN_HEADER, Controller, create_controller_app, refuse_unsafe

TOKEN = "test-token-123"
LOCAL = "http://127.0.0.1:8090"
TERMINAL = {"passed", "failed", "error"}


class FakeDocker:
    """Records docker invocations; `running` controls what `inspect` reports."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.running = True

    def __call__(
        self, args: list[str], env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        self.envs.append(env)
        out = ""
        if args[:2] == ["inspect", "-f"]:
            out = ("true" if self.running else "false") if "Running" in args[2] else "0"
        return subprocess.CompletedProcess(args, 0, out, "")

    def compose(self) -> list[list[str]]:
        return [c[1:] for c in self.calls if c[0] == "compose"]


def outcome(ok: bool = True) -> ExperimentOutcome:
    return ExperimentOutcome(
        summary="fake", explanation=[], metrics=[], checks=[check("x", "x", ok, "observed")]
    )


async def stops_redis(ctx: ExperimentContext, params, env: Env) -> ExperimentOutcome:  # type: ignore[no-untyped-def]
    await env.infra.stop("redis")
    return outcome()


async def stops_then_fails(ctx: ExperimentContext, params, env: Env) -> ExperimentOutcome:  # type: ignore[no-untyped-def]
    await env.infra.stop("rabbitmq")
    raise RuntimeError("boom in the middle of the outage")


async def stops_then_hangs(ctx: ExperimentContext, params, env: Env) -> ExperimentOutcome:  # type: ignore[no-untyped-def]
    await env.infra.stop("api-2")
    await asyncio.sleep(30)
    return outcome()


RELEASE = {"event": None}


async def blocks(ctx: ExperimentContext, params, env: Env) -> ExperimentOutcome:  # type: ignore[no-untyped-def]
    while not RELEASE["event"]:
        await asyncio.sleep(0.05)
    return outcome()


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def journal() -> Iterator[Journal]:
    with tempfile.TemporaryDirectory(prefix="labctl-test-") as directory:
        yield Journal(Path(directory) / "journal.json")


def make(
    settings: Settings, docker: FakeDocker, journal: Journal, *, enabled: bool = True, **kw
) -> Controller:  # type: ignore[no-untyped-def]
    return Controller(
        settings,
        failures_enabled=enabled,
        token=TOKEN,
        journal=journal,
        command_runner=docker,
        experiments=kw.pop("experiments", {"redis_outage": stops_redis}),
        **kw,
    )


@pytest.fixture
def controller(
    test_settings: Settings, engine: Engine, docker: FakeDocker, journal: Journal
) -> Iterator[Controller]:
    c = make(test_settings, docker, journal)
    yield c
    c.runner.wait_idle(30)
    c.close()


def client_for(c: Controller, **headers: str) -> TestClient:
    return TestClient(
        create_controller_app(c), base_url=LOCAL, headers={TOKEN_HEADER: TOKEN, **headers}
    )


def start(client: TestClient, experiment: str = "redis_outage", **body) -> object:  # type: ignore[no-untyped-def]
    return client.post(
        "/controller/runs", json={"experiment": experiment, "params": {}, "confirm": True, **body}
    )


def wait_terminal(factory: sessionmaker[Session], run_id: str, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with factory() as session:
            row = (
                session.execute(
                    text("SELECT status, error, checks FROM lab_runs WHERE id = :id"),
                    {"id": run_id},
                )
                .mappings()
                .one()
            )
        if row["status"] in TERMINAL:
            return dict(row)
        time.sleep(0.1)
    raise AssertionError("run did not finish")


# --- access control -------------------------------------------------------------------------


def test_requests_need_the_token_a_local_host_and_a_local_origin(controller: Controller) -> None:
    app = create_controller_app(controller)
    with TestClient(app, base_url=LOCAL) as anonymous:
        assert anonymous.get("/controller/status").status_code == 401
        wrong = anonymous.get("/controller/status", headers={TOKEN_HEADER: "nope"})
        assert wrong.status_code == 401
    with client_for(controller) as ok:
        assert ok.get("/controller/status").status_code == 200
        # A foreign web page (Origin) or a rebound DNS name (Host) is refused even WITH a token.
        assert (
            ok.get("/controller/status", headers={"origin": "https://evil.example"}).status_code
            == 403
        )
        assert (
            ok.get("/controller/status", headers={"host": "evil.example:8090"}).status_code == 403
        )
        # CORS preflights are never answered positively.
        assert ok.options("/controller/runs").status_code == 403
        assert (
            ok.get("/controller/status", headers={"origin": "http://127.0.0.1:5173"}).status_code
            == 200
        )


def test_disabled_failures_cannot_run(
    test_settings: Settings, engine: Engine, docker, journal
) -> None:  # type: ignore[no-untyped-def]
    c = make(test_settings, docker, journal, enabled=False)
    try:
        with client_for(c) as client:
            status = client.get("/controller/status").json()
            assert (status["failures_enabled"], status["experiments"]) == (False, [])
            assert start(client).status_code == 403
        assert docker.calls == []
    finally:
        c.close()


def test_refuses_production_and_non_local_databases(test_settings: Settings) -> None:
    with pytest.raises(SystemExit):
        refuse_unsafe(
            test_settings.model_copy(
                update={"app_env": "production", "enable_dev_endpoints": False}
            )
        )
    with pytest.raises(SystemExit):
        refuse_unsafe(test_settings.model_copy(update={"postgres_host": "db.example.com"}))


@pytest.mark.parametrize(
    "body",
    [
        {"experiment": "docker_exec", "params": {}, "confirm": True},  # not on the allow-list
        {"experiment": "concurrency", "params": {}, "confirm": True},  # API experiment, not here
        {"experiment": "redis_outage", "params": {"outage_seconds": 999}, "confirm": True},
        {"experiment": "redis_outage", "params": {"service": "postgres"}, "confirm": True},
        {"experiment": "redis_outage", "params": {}},  # no explicit confirmation
        {"experiment": "redis_outage", "params": {}, "confirm": False},
        {"experiment": "redis_outage", "params": {}, "confirm": True, "command": "rm -rf /"},
    ],
)
def test_invalid_requests_are_rejected_before_anything_runs(
    controller: Controller, docker, body
) -> None:  # type: ignore[no-untyped-def]
    with client_for(controller) as client:
        assert client.post("/controller/runs", json=body).status_code == 422
    assert docker.calls == []


# --- allow-list ---------------------------------------------------------------------------


def test_infra_allow_list(journal: Journal, docker: FakeDocker) -> None:
    infra = Infra("run", journal, docker)
    for service in ("postgres", "nginx", "api-1", "ledgerflow-redis", "redis; rm -rf /"):
        with pytest.raises(InfraNotAllowedError):
            asyncio.run(infra.stop(service))
    with pytest.raises(InfraNotAllowedError):
        asyncio.run(infra.recreate_consumer({"PATH": "/tmp"}))
    with pytest.raises(InfraNotAllowedError):
        asyncio.run(infra.recreate_consumer({"CONSUMER_CRASH_AFTER_COMMIT_EVENT": "not-a-uuid"}))
    with pytest.raises(InfraNotAllowedError):
        asyncio.run(infra.queue_depth("other.queue"))
    assert docker.calls == []
    assert journal.load() is None


def test_fault_injection_env_is_exactly_the_two_hooks(journal: Journal, docker: FakeDocker) -> None:
    infra = Infra("run", journal, docker)
    event = str(uuid.uuid4())
    asyncio.run(infra.recreate_consumer({"CONSUMER_CRASH_BEFORE_COMMIT_EVENT": event}))
    assert docker.compose()[-1] == ["up", "-d", "--no-deps", "event-consumer"]
    assert docker.envs[-1] == {
        "CONSUMER_CRASH_BEFORE_COMMIT_EVENT": event,
        "CONSUMER_CRASH_AFTER_COMMIT_EVENT": "",
    }
    asyncio.run(infra.restore())  # restoration clears the hook again
    last_up = max(i for i, c in enumerate(docker.calls) if c[:2] == ["compose", "up"])
    assert last_up > 0 and docker.envs[last_up] == {
        "CONSUMER_CRASH_BEFORE_COMMIT_EVENT": "",
        "CONSUMER_CRASH_AFTER_COMMIT_EVENT": "",
    }
    assert journal.load() is None


# --- lifecycle: success, failure, timeout, exclusivity ------------------------------------


def test_cleanup_after_success(
    controller: Controller, docker: FakeDocker, journal: Journal, session_factory
) -> None:  # type: ignore[no-untyped-def]
    with client_for(controller) as client:
        response = start(client)
        assert response.status_code == 202
        run = wait_terminal(session_factory, response.json()["id"])
    assert run["status"] == "passed"
    assert ["stop", "redis"] in docker.compose()
    assert docker.compose()[-1] == ["start", "redis"]  # restored afterwards
    assert journal.load() is None


def test_cleanup_after_failure(test_settings, engine, docker, journal, session_factory) -> None:  # type: ignore[no-untyped-def]
    c = make(test_settings, docker, journal, experiments={"rabbitmq_outage": stops_then_fails})
    try:
        with client_for(c) as client:
            run = wait_terminal(session_factory, start(client, "rabbitmq_outage").json()["id"])
        assert run["status"] == "error" and "boom" in run["error"]
        assert docker.compose()[-1] == ["start", "rabbitmq"]
        assert journal.load() is None
    finally:
        c.runner.wait_idle(30)
        c.close()


def test_timeout_stops_the_run_and_still_restores(
    test_settings, engine, docker, journal, session_factory
) -> None:  # type: ignore[no-untyped-def]
    c = make(
        test_settings,
        docker,
        journal,
        experiments={"replica_failure": stops_then_hangs},
        run_timeout_seconds=1,
    )
    try:
        with client_for(c) as client:
            run = wait_terminal(session_factory, start(client, "replica_failure").json()["id"])
        assert run["status"] == "error" and "limit" in run["error"]
        assert docker.compose()[-1] == ["start", "api-2"]
        assert journal.load() is None
    finally:
        c.runner.wait_idle(30)
        c.close()


def test_recovery_failure_is_reported_explicitly(
    controller: Controller, docker: FakeDocker, journal: Journal, session_factory
) -> None:  # type: ignore[no-untyped-def]
    docker.running = False  # the service does not come back
    with client_for(controller) as client:
        run = wait_terminal(session_factory, start(client).json()["id"])
    assert run["status"] == "error"
    assert run["error"].startswith("RecoveryFailedError: RECOVERY FAILED")
    assert journal.load() is not None  # kept, so a later start can try again


def test_only_one_run_at_a_time(test_settings, engine, docker, journal, session_factory) -> None:  # type: ignore[no-untyped-def]
    RELEASE["event"] = None
    c = make(
        test_settings,
        docker,
        journal,
        experiments={"worker_crash": blocks, "redis_outage": stops_redis},
    )
    try:
        with client_for(c) as client:
            first = start(client, "worker_crash")
            assert first.status_code == 202
            assert start(client).status_code == 409
            RELEASE["event"] = True
            wait_terminal(session_factory, first.json()["id"])
    finally:
        RELEASE["event"] = True
        c.runner.wait_idle(30)
        c.close()


def test_an_api_lab_run_also_blocks_the_controller(
    controller: Controller, docker, session_factory, engine
) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO lab_runs (id, experiment, status, params, progress, runner_replica, "
                "target_base_url) VALUES (:id, 'concurrency', 'running', '{}', '{}', 'api-1', 'x')"
            ),
            {"id": uuid.uuid4()},
        )
    with client_for(controller) as client:
        assert start(client).status_code == 409
    assert docker.calls == []


def test_interrupted_run_is_restored_on_startup(
    test_settings, engine, docker, journal, session_factory
) -> None:  # type: ignore[no-untyped-def]
    run_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO lab_runs (id, experiment, status, params, progress, runner_replica, "
                "target_base_url) VALUES (:id, 'redis_outage', 'running', '{}', '{}', "
                "'lab-controller', 'x')"
            ),
            {"id": run_id},
        )
    journal.add(str(run_id), "redis", "stopped")  # the controller died mid-outage
    c = make(test_settings, docker, journal)
    try:
        c.recover_interrupted()
        assert docker.compose() == [["start", "redis"]]
        assert journal.load() is None
        with session_factory() as session:
            status, error = session.execute(
                text("SELECT status, error FROM lab_runs WHERE id = :id"), {"id": run_id}
            ).one()
        assert status == "error" and "restored: redis" in error
        assert c.status().interrupted_recovery == {
            "run_id": str(run_id),
            "restored": ["redis"],
            "ok": True,
        }
    finally:
        c.close()


# --- benchmarks (Performance Lab) --------------------------------------------------------------


class FakeProcess:
    def __init__(self, lines: list[str], code: int = 0) -> None:
        self.stdout = iter(f"{line}\n" for line in lines)
        self._code = code

    def wait(self) -> int:
        return self._code

    def kill(self) -> None:
        pass

    def poll(self) -> int:
        return self._code


class FakePopen:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, cmd, **kwargs):  # type: ignore[no-untyped-def]
        self.commands.append(cmd)
        return FakeProcess(["== 1 replica(s)", "Wrote benchmarks/results/20261120-120000.md"])


def bench_body(**overrides) -> dict:  # type: ignore[no-untyped-def]
    return {"duration_s": 10, "users": 8, "replicas": [2, 1], "confirm": True, **overrides}


def test_benchmarks_are_disabled_by_default(controller: Controller) -> None:
    with client_for(controller) as client:
        assert client.post("/controller/benchmarks", json=bench_body()).status_code == 403
        assert client.get("/controller/status").json()["benchmarks_enabled"] is False


@pytest.mark.parametrize(
    "body",
    [
        bench_body(users=500),
        bench_body(duration_s=600),
        bench_body(replicas=[3]),
        bench_body(replicas=[1, 1]),
        bench_body(replicas=[]),
        bench_body(confirm=False),
        bench_body(database="ledgerflow"),  # the benchmark database is not selectable
    ],
)
def test_benchmark_parameters_are_bounded(test_settings, engine, docker, journal, body) -> None:  # type: ignore[no-untyped-def]
    popen = FakePopen()
    from labctl.benchmarks import BenchmarkJob

    c = make(
        test_settings, docker, journal, benchmarks_enabled=True, benchmark_job=BenchmarkJob(popen)
    )
    try:
        with client_for(c) as client:
            assert client.post("/controller/benchmarks", json=body).status_code == 422
        assert popen.commands == []
    finally:
        c.close()


def test_benchmark_runs_the_existing_harness_with_fixed_arguments(
    test_settings, engine, docker, journal
) -> None:  # type: ignore[no-untyped-def]
    popen = FakePopen()
    from labctl.benchmarks import BenchmarkJob

    c = make(
        test_settings, docker, journal, benchmarks_enabled=True, benchmark_job=BenchmarkJob(popen)
    )
    try:
        with client_for(c) as client:
            started = client.post("/controller/benchmarks", json=bench_body())
            assert started.status_code == 202
            deadline = time.monotonic() + 10
            while client.get("/controller/benchmarks/current").json()["state"] == "running":
                assert time.monotonic() < deadline
                time.sleep(0.05)
            status = client.get("/controller/benchmarks/current").json()
        assert status["state"] == "succeeded"
        assert status["result_file"] == "benchmarks/results/20261120-120000.md"
        cmd = popen.commands[0]
        assert cmd[1].replace("\\", "/").endswith("benchmarks/run_benchmarks.py")
        assert cmd[2:] == [
            "--duration", "10", "--users", "8", "--replicas", "1", "2",
            "--source", "controller", "--skip-quota-run",
        ]  # fmt: skip
    finally:
        c.close()


def test_benchmark_never_overlaps_an_experiment(
    test_settings, engine, docker, journal, session_factory
) -> None:  # type: ignore[no-untyped-def]
    popen = FakePopen()
    from labctl.benchmarks import BenchmarkJob

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO lab_runs (id, experiment, status, params, progress, runner_replica, "
                "target_base_url) VALUES (:id, 'concurrency', 'running', '{}', '{}', 'api-1', 'x')"
            ),
            {"id": uuid.uuid4()},
        )
    c = make(
        test_settings, docker, journal, benchmarks_enabled=True, benchmark_job=BenchmarkJob(popen)
    )
    try:
        with client_for(c) as client:
            assert client.post("/controller/benchmarks", json=bench_body()).status_code == 409
        assert popen.commands == []
    finally:
        c.close()
