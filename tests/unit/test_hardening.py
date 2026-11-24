"""Phase 6A regression tests: secrets, dev-surface exposure, broker probe, model integrity,
benchmark isolation. Pure unit tests: no database, broker or Docker daemon required
(the Compose-config comparison only needs the docker CLI and is skipped without it)."""

import hashlib
import hmac
import importlib.util
import json
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import LAB_SECRET_MIN_LENGTH, Settings, is_loopback_host
from app.lab.signing import LabSigningNotConfiguredError, LabTrafficSigner
from app.observability.topology import TopologyProbe

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import stack_control  # noqa: E402

GOOD_SECRET = "q7Lr2vXk9PzT4mWb8NcY3hJd6FsA1eGu5KoR0iVt"  # fake test key  gitleaks:allow


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"postgres_user": "u", "postgres_password": "s3cr3t-db-password"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# --- lab signing secret ------------------------------------------------------------------------


def test_dev_endpoints_require_an_independent_lab_secret() -> None:
    with pytest.raises(ValidationError, match="LAB_TRAFFIC_SECRET is not set"):
        make_settings(enable_dev_endpoints=True)
    settings = make_settings(enable_dev_endpoints=True, lab_traffic_secret=GOOD_SECRET)
    assert settings.lab_traffic_secret is not None


@pytest.mark.parametrize("empty", ["", "   "])
def test_an_empty_lab_secret_counts_as_missing(empty: str) -> None:
    # Compose passes `LAB_TRAFFIC_SECRET: ${LAB_TRAFFIC_SECRET:-}` -> "" when unset.
    assert make_settings(lab_traffic_secret=empty).lab_traffic_secret is None
    with pytest.raises(ValidationError, match="not set"):
        make_settings(enable_dev_endpoints=True, lab_traffic_secret=empty)


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("short-secret", f"at least {LAB_SECRET_MIN_LENGTH}"),
        ("a" * 64, "placeholder"),
        ("abababababababababababababababababab", "placeholder"),
    ],
)
def test_weak_lab_secrets_are_rejected(secret: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(enable_dev_endpoints=True, lab_traffic_secret=secret)


@pytest.mark.parametrize("field", ["postgres_password", "rabbitmq_password"])
def test_lab_secret_must_differ_from_other_credentials(field: str) -> None:
    with pytest.raises(ValidationError, match="independent of every other credential"):
        make_settings(
            enable_dev_endpoints=True, lab_traffic_secret=GOOD_SECRET, **{field: GOOD_SECRET}
        )


def test_signer_has_no_fallback_key_even_when_validation_is_bypassed() -> None:
    # model_copy skips validators: the signer must check on its own.
    unchecked = make_settings().model_copy(update={"enable_dev_endpoints": True})
    with pytest.raises(LabSigningNotConfiguredError, match="LAB_TRAFFIC_SECRET"):
        LabTrafficSigner.from_settings(unchecked)
    weak = unchecked.model_copy(update={"lab_traffic_secret": SecretStr("too-short")})
    with pytest.raises(LabSigningNotConfiguredError):
        LabTrafficSigner.from_settings(weak)


def test_tokens_from_the_old_password_derived_key_are_rejected() -> None:
    settings = make_settings(enable_dev_endpoints=True, lab_traffic_secret=GOOD_SECRET)
    signer = LabTrafficSigner.from_settings(settings)
    # What the removed fallback would have produced from POSTGRES_PASSWORD.
    legacy_key = hmac.new(
        settings.postgres_password.get_secret_value().encode(),
        b"ledgerflow-lab-traffic-v1",
        hashlib.sha256,
    ).digest()
    forged = LabTrafficSigner(legacy_key).token(uuid.uuid4(), "burst")
    assert signer.verify(forged) is None
    assert signer.principal_for(forged) is None
    genuine = signer.token(run_id := uuid.uuid4(), "standard")
    assert signer.verify(genuine) == (run_id, "standard")


def test_controller_refuses_to_start_without_a_lab_secret() -> None:
    from labctl.server import refuse_unsafe

    with pytest.raises(SystemExit, match="LAB_TRAFFIC_SECRET"):
        refuse_unsafe(make_settings())
    refuse_unsafe(make_settings(lab_traffic_secret=GOOD_SECRET))  # local + secret: allowed


# --- development surface exposure -------------------------------------------------------------


@pytest.mark.parametrize("bind", ["0.0.0.0", "", "192.168.1.20", "::", "ledgerflow.example.com"])
def test_dev_endpoints_are_refused_on_a_non_loopback_gateway(bind: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        make_settings(enable_dev_endpoints=True, lab_traffic_secret=GOOD_SECRET, gateway_bind=bind)


@pytest.mark.parametrize("bind", ["127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]"])
def test_dev_endpoints_are_allowed_on_loopback(bind: str) -> None:
    assert is_loopback_host(bind)
    make_settings(enable_dev_endpoints=True, lab_traffic_secret=GOOD_SECRET, gateway_bind=bind)


def test_a_public_gateway_is_fine_without_dev_endpoints() -> None:
    assert make_settings(gateway_bind="0.0.0.0").enable_dev_endpoints is False


@pytest.mark.parametrize(
    "unsafe",
    [
        {"enable_dev_endpoints": True, "lab_traffic_secret": GOOD_SECRET},
        {"expose_replica_header": True},
        {"db_echo": True},
        {"rate_limit_backend": "memory"},
        {"consumer_crash_before_commit_event": str(uuid.uuid4())},
    ],
)
def test_production_refuses_every_development_aid(unsafe: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        make_settings(app_env="production", **unsafe)


def test_a_minimal_production_configuration_is_accepted() -> None:
    settings = make_settings(app_env="production", gateway_bind="0.0.0.0")
    assert settings.enable_dev_endpoints is False
    assert settings.lab_traffic_secret is None  # not needed: the lab is not mounted


def test_production_app_mounts_no_development_routes() -> None:
    from app.main import create_app
    from app.rate_limiting import InMemoryFixedWindowLimiter

    app = create_app(
        make_settings(app_env="production", rate_limit_backend="redis"),
        rate_limiter=InMemoryFixedWindowLimiter(),
    )
    paths = {getattr(route, "path", "") for route in app.routes}
    dev_prefixes = ("/api/v1/lab", "/api/v1/observability", "/api/v1/ai", "/api/v1/performance")
    assert not [p for p in paths if p.startswith(dev_prefixes) or p.endswith("/fund")]
    assert app.state.lab_runner is None


# --- RabbitMQ: least-privilege probe ----------------------------------------------------------


def _probe(handler, **overrides: Any) -> TopologyProbe:  # type: ignore[no-untyped-def]
    settings = make_settings(
        rabbitmq_management_url="http://rabbitmq:15672",
        rabbitmq_monitor_password="monitor-pass",
        **overrides,
    )
    return TopologyProbe(
        settings=settings,
        session_factory=None,  # type: ignore[arg-type]  # the broker probe never uses it
        redis_client=None,
        tracing_enabled=False,
        http_transport=httpx.MockTransport(handler),
    )


QUEUES = [
    {"name": "ledgerflow.ledger-audit", "messages_ready": 2, "messages_unacknowledged": 1},
    {"name": "ledgerflow.ledger-audit.retry.1", "messages_ready": 0},
    {"name": "ledgerflow.ledger-audit.dlq", "messages_ready": 3, "messages_unacknowledged": 0},
    {"name": "ledgerflow.anomaly-detector.dlq", "messages_ready": 1},
    {"name": "test-abc.ledger-audit", "messages_ready": 99},  # another prefix: ignored
]


def _broker_api(alarm_status: int, seen: list[httpx.Request]):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/health/checks/alarms":
            return httpx.Response(alarm_status, json={"status": "ok"})
        if request.url.raw_path.decode().startswith("/api/queues/%2F"):
            return httpx.Response(200, json=QUEUES)
        return httpx.Response(404)

    return handler


def test_broker_probe_reports_health_and_dead_letters_with_monitoring_credentials() -> None:
    seen: list[httpx.Request] = []
    probe = _probe(_broker_api(200, seen))
    result = probe.probe_broker()
    assert result is not None and result.state == "ok"
    assert [q.name for q in result.queues] == sorted(q["name"] for q in QUEUES[:4])
    assert result.dead_lettered == 4
    assert {q.role for q in result.queues} == {"main", "retry", "dead_letter"}
    # Read-only GETs, authenticated as the monitoring user only.
    assert {r.method for r in seen} == {"GET"}
    expected = (
        "Basic " + __import__("base64").b64encode(b"ledgerflow_monitor:monitor-pass").decode()
    )
    assert all(r.headers["authorization"] == expected for r in seen)

    component = probe._broker_probed([], result)
    assert (component.status, component.evidence) == ("healthy", "probe")
    assert "4 dead-lettered" in component.message
    metrics = {m.key: m.value for m in component.metrics}
    assert metrics["dead_lettered"] == 4


@pytest.mark.parametrize(
    ("alarm_status", "state", "status"),
    [(503, "alarm", "degraded"), (401, "auth", "unknown"), (500, "down", "unhealthy")],
)
def test_broker_probe_maps_failures(alarm_status: int, state: str, status: str) -> None:
    probe = _probe(_broker_api(alarm_status, []))
    result = probe.probe_broker()
    assert result is not None and result.state == state
    assert probe._broker_probed(None, result).status == status


def test_broker_probe_reports_an_unreachable_broker_as_unhealthy() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    probe = _probe(refuse)
    result = probe.probe_broker()
    assert result is not None and result.state == "down"
    component = probe._broker_probed(None, result)
    assert component.status == "unhealthy"
    assert "outbox" in component.message  # transfers still commit


def test_without_monitor_credentials_there_is_no_independent_probe() -> None:
    probe = TopologyProbe(
        settings=make_settings(rabbitmq_management_url="http://rabbitmq:15672"),
        session_factory=None,  # type: ignore[arg-type]
        redis_client=None,
        tracing_enabled=False,
        http_transport=httpx.MockTransport(lambda r: pytest.fail("must not probe")),
    )
    assert probe.probe_broker() is None


def test_worker_reports_are_shown_next_to_the_probe() -> None:
    probe = _probe(_broker_api(200, []))
    now = datetime.now(UTC)
    rows = [
        {
            "component": "event-consumer",
            "instance": "c1",
            "details": {"broker_connected": True},
            "next_beat_before": now,
            "db_now": now,
        },
        {
            "component": "outbox-publisher",
            "instance": "p1",
            "details": {"broker_connected": False},
            "next_beat_before": now,
            "db_now": now,
        },
    ]
    result = probe.probe_broker()
    assert result is not None
    metrics = {m.key: m.value for m in probe._broker_probed(rows, result).metrics}
    assert metrics["workers_connected"] == "1/2"


class FakeManagementApi:
    def __init__(self, *, leftover_permissions: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.leftover = leftover_permissions

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.raw_path.decode()))
        path = request.url.raw_path.decode()
        if request.method == "PUT" and path.startswith("/api/users/"):
            assert json.loads(request.content) == {"password": "pw", "tags": "monitoring"}
            return httpx.Response(201)
        if request.method == "PUT":
            assert path == "/api/permissions/%2F/mon"
            assert json.loads(request.content) == {"configure": "^$", "write": "^$", "read": "^$"}
            return httpx.Response(201)
        if path == "/api/vhosts":
            return httpx.Response(200, json=[{"name": "/"}, {"name": "other"}])
        if request.method == "DELETE":
            return httpx.Response(404)
        if path.endswith("/permissions"):
            granted = {"vhost": "/", "configure": "^$", "write": "^$", "read": "^$"}
            if self.leftover:
                return httpx.Response(200, json=[granted, {"vhost": "other", "read": ".*"}])
            return httpx.Response(200, json=[granted])
        return httpx.Response(200, json={"name": "mon", "tags": ["monitoring"]})


def test_monitor_setup_grants_only_the_monitoring_tag_and_deny_all_access() -> None:
    from app.events.broker_monitor_setup import provision

    api = FakeManagementApi()
    with httpx.Client(base_url="http://rabbitmq:15672", transport=httpx.MockTransport(api)) as c:
        provision(c, user="mon", password="pw")
    deletes = [path for method, path in api.calls if method == "DELETE"]
    assert deletes == [
        "/api/topic-permissions/%2F/mon",
        "/api/permissions/other/mon",
        "/api/topic-permissions/other/mon",
    ]
    assert ("PUT", "/api/permissions/%2F/mon") in api.calls


def test_monitor_setup_fails_if_permissions_remain() -> None:
    from app.events.broker_monitor_setup import SetupError, provision

    api = FakeManagementApi(leftover_permissions=True)
    with (
        httpx.Client(base_url="http://rabbitmq:15672", transport=httpx.MockTransport(api)) as c,
        pytest.raises(SetupError, match="permissions"),
    ):
        provision(c, user="mon", password="pw")


def test_monitor_setup_is_a_no_op_without_a_monitor_password(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.events import broker_monitor_setup

    monkeypatch.setattr(broker_monitor_setup, "Settings", lambda: make_settings())
    assert broker_monitor_setup.main() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"rabbitmq_monitor_user": "ledgerflow"},  # same as the admin user
        {"rabbitmq_monitor_password": "admin-pass"},  # same as the admin password
    ],
)
def test_monitor_setup_refuses_to_reuse_admin_credentials(monkeypatch, overrides) -> None:  # type: ignore[no-untyped-def]
    from app.events import broker_monitor_setup

    values = {
        "rabbitmq_management_url": "http://rabbitmq:15672",
        "rabbitmq_password": "admin-pass",
        "rabbitmq_monitor_password": "monitor-pass",
        **overrides,
    }
    monkeypatch.setattr(broker_monitor_setup, "Settings", lambda: make_settings(**values))
    assert broker_monitor_setup.main() == 1


# --- model artifact integrity ---------------------------------------------------------------


def test_shipped_artifact_matches_its_recorded_digest() -> None:
    from app.ai.model import ARTIFACT, artifact_digest, load_metadata, verify_artifact

    meta = load_metadata()
    assert meta is not None
    assert meta["artifact_sha256"] == artifact_digest(ARTIFACT)
    verify_artifact(ARTIFACT, meta, meta["environment"]["scikit_learn"])


def test_tampered_unverified_or_mismatched_artifacts_are_never_unpickled(tmp_path: Path) -> None:
    from app.ai.model import ARTIFACT, ModelIntegrityError, load_metadata, verify_artifact

    meta = load_metadata()
    assert meta is not None
    version = meta["environment"]["scikit_learn"]
    tampered = tmp_path / "model.joblib"
    tampered.write_bytes(ARTIFACT.read_bytes() + b"\x00")
    with pytest.raises(ModelIntegrityError, match="does not match"):
        verify_artifact(tampered, meta, version)
    with pytest.raises(ModelIntegrityError, match="no artifact_sha256"):
        verify_artifact(
            ARTIFACT, {k: v for k, v in meta.items() if k != "artifact_sha256"}, version
        )
    with pytest.raises(ModelIntegrityError, match="scikit-learn"):
        verify_artifact(ARTIFACT, meta, "0.0.1")


def test_model_load_verifies_before_unpickling(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("joblib")
    from app.ai import model as model_module

    tampered = tmp_path / "model.joblib"
    tampered.write_bytes(b"not the artifact")
    monkeypatch.setattr("joblib.load", lambda *_: pytest.fail("unpickled an unverified file"))
    with pytest.raises(model_module.ModelIntegrityError):
        model_module.AnomalyModel.load(artifact=tampered)


# --- benchmark isolation ------------------------------------------------------------------------


def test_the_benchmark_project_is_isolated_from_the_dev_stack() -> None:
    stack_control.assert_isolated(stack_control.BENCH)  # does not raise
    bench = stack_control.BENCH
    assert bench.name != stack_control.DEV.name
    assert bench.network == "ledgerflow-bench_backend"
    assert bench.container("postgres") == "ledgerflow-bench-postgres"
    assert bench.env["API_POSTGRES_DB"] == bench.env["POSTGRES_DB"] == "ledgerflow_bench"
    assert ":8180" in bench.base_url and ":6479" in bench.redis_url


@pytest.mark.parametrize(
    "change",
    [
        {"name": "ledgerflow"},
        {"container_prefix": "ledgerflow"},
        {"base_url": "http://127.0.0.1:8080"},
        {"redis_url": "redis://127.0.0.1:6379/0"},
        {"env_update": {"BACKEND_NET": "172.28.0"}},
        {"env_update": {"POSTGRES_PORT": "5432"}},
        {"env_update": {"NGINX_PORT": "8080"}},
        {"env_update": {"API_POSTGRES_DB": "ledgerflow"}},
        {"env_update": {"NGINX_BIND": "0.0.0.0"}},
        {"env_drop": "STACK_CONTAINER_PREFIX"},
    ],
)
def test_any_overlap_with_the_dev_stack_is_refused(change: dict[str, Any]) -> None:
    import dataclasses

    bench = stack_control.BENCH
    env = dict(bench.env)
    env.update(change.pop("env_update", {}))
    if drop := change.pop("env_drop", None):
        env.pop(drop)
    candidate = dataclasses.replace(bench, env=env, **change)
    with pytest.raises(SystemExit, match="Refusing"):
        stack_control.assert_isolated(candidate)


def test_overrides_cannot_point_the_benchmark_back_at_the_dev_stack(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ledgerflow")
    monkeypatch.setenv("NGINX_PORT", "8080")
    env = stack_control.compose_env(
        stack_control.BENCH, {"API_POSTGRES_DB": "ledgerflow", "POSTGRES_PORT": "5432"}
    )
    assert "COMPOSE_PROJECT_NAME" not in env
    assert (env["API_POSTGRES_DB"], env["POSTGRES_PORT"], env["NGINX_PORT"]) == (
        "ledgerflow_bench",
        "5532",
        "8180",
    )
    cmd = stack_control.compose_command(stack_control.BENCH, ("up", "-d"))
    assert cmd[:4] == ["docker", "compose", "-p", "ledgerflow-bench"]


def test_teardown_never_touches_the_dev_project(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ran docker"))
    with pytest.raises(SystemExit):
        stack_control.teardown(stack_control.DEV)


def _load_harness():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "run_benchmarks", ROOT / "benchmarks" / "run_benchmarks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_harness_only_ever_deploys_the_benchmark_project(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    harness = _load_harness()
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        harness.stack_control, "remove_extra_replicas", lambda p=None: calls.append(("rm", p))
    )
    monkeypatch.setattr(
        harness.stack_control,
        "deploy",
        lambda env, **kw: calls.append(("deploy", (env, kw))),
    )
    monkeypatch.setattr(
        harness.stack_control, "compose", lambda *a, **kw: calls.append(("compose", kw))
    )
    monkeypatch.setattr(
        harness.stack_control,
        "wait_until_served_only_by",
        lambda url, *a, **k: calls.append(("wait", url)),
    )
    harness.configure(1, harness.BENCH_ENV)
    harness.configure(4, harness.SMALL_QUOTA)
    bench = stack_control.BENCH
    for kind, value in calls:
        if kind == "rm":
            assert value is bench
        elif kind == "deploy":
            env, kw = value
            assert kw["project"] is bench
            assert "API_POSTGRES_DB" not in env  # comes only from the project definition
        elif kind == "compose":
            assert value["project"] is bench
        else:
            assert value == bench.base_url
    deploys = [v for k, v in calls if k == "deploy"]
    assert deploys[1][1]["services"] == (*bench.services, "api-3", "api-4")


def test_controller_timeout_cleanup_removes_only_the_benchmark_project() -> None:
    from labctl.benchmarks import RESTORE_SNIPPET

    assert "teardown(stack_control.BENCH)" in RESTORE_SNIPPET
    assert "restore_default" not in RESTORE_SNIPPET


def _compose_config(project: stack_control.Project) -> dict[str, Any]:
    env = stack_control.compose_env(
        project,
        {
            "POSTGRES_USER": "u",
            "POSTGRES_PASSWORD": "p",
            "RABBITMQ_PASSWORD": "r",
            "APP_DB_PASSWORD": "a",
            "NGINX_UPSTREAMS": "4",
        },
    )
    result = subprocess.run(
        [
            *stack_control.compose_command(project, ()),
            "--profile",
            "scale4",
            "--profile",
            "observability",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {result.stderr[-300:]}")
    return json.loads(result.stdout)


def _facts(config: dict[str, Any]) -> dict[str, set[str]]:
    services = config["services"].values()
    return {
        "containers": {s["container_name"] for s in services if s.get("container_name")},
        "ports": {
            f"{p.get('host_ip')}:{p['published']}" for s in services for p in s.get("ports", [])
        },
        "networks": {n["name"] for n in config["networks"].values()},
        "subnets": {c["subnet"] for n in config["networks"].values() for c in n["ipam"]["config"]},
        "volumes": {v["name"] for v in config.get("volumes", {}).values()},
    }


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
@pytest.mark.parametrize(
    ("first", "second", "database"),
    [
        ("DEV", "BENCH", "ledgerflow_bench"),
        ("DEV", "STACK", "ledgerflow_stacktest"),
        ("BENCH", "STACK", "ledgerflow_stacktest"),
    ],
)
def test_rendered_compose_projects_share_no_container_port_network_or_volume(
    first: str, second: str, database: str
) -> None:
    a = _compose_config(getattr(stack_control, first))
    b = _compose_config(getattr(stack_control, second))
    a_facts, b_facts = _facts(a), _facts(b)
    assert a["name"] != b["name"]
    for key in a_facts:
        assert a_facts[key], key
        assert not a_facts[key] & b_facts[key], f"shared {key}: {a_facts[key] & b_facts[key]}"
    # Every published port of an isolated project is loopback-only.
    assert all(p.startswith("127.0.0.1:") for p in b_facts["ports"])
    assert b["services"]["api-1"]["environment"]["POSTGRES_DB"] == database


def test_the_stack_test_project_is_isolated_from_dev_and_bench() -> None:
    stack_control.assert_isolated(stack_control.STACK)
    stack_control.assert_isolated(stack_control.STACK, stack_control.BENCH)
    assert stack_control.STACK.services == ()  # complete stack: broker, workers, AI worker
    assert stack_control.STACK.service_container("api-2") == "ledgerflow-stacktest-api-2-1"


def test_app_factory_refuses_the_lab_without_a_signing_key() -> None:
    from app.main import create_app
    from app.rate_limiting import InMemoryFixedWindowLimiter

    unchecked = make_settings(rate_limit_backend="memory").model_copy(
        update={"enable_dev_endpoints": True}
    )
    with pytest.raises(LabSigningNotConfiguredError):
        create_app(unchecked, rate_limiter=InMemoryFixedWindowLimiter())


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_the_public_demo_profile_exposes_no_data_store_and_no_dev_surface() -> None:
    env = stack_control.compose_env(
        stack_control.DEV,
        {
            "POSTGRES_USER": "u",
            "POSTGRES_PASSWORD": "p",
            "RABBITMQ_PASSWORD": "r",
            "APP_DB_PASSWORD": "a",
            "DEMO_BIND": "0.0.0.0",
        },
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.demo.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {result.stderr[-300:]}")
    services = json.loads(result.stdout)["services"]
    published = {name for name, s in services.items() if s.get("ports")}
    assert published == {"nginx", "frontend"}  # no PostgreSQL, Redis or RabbitMQ port
    for name in ("api-1", "api-2", "outbox-publisher", "event-consumer", "ai-worker", "migrate"):
        environment = services[name]["environment"]
        assert environment["APP_ENV"] == "production", name
        assert environment["ENABLE_DEV_ENDPOINTS"] == "false", name
    # With a public bind, the replicas also learn it, so the dev surface is doubly refused.
    assert services["api-1"]["environment"]["GATEWAY_BIND"] == "0.0.0.0"
    with pytest.raises(ValidationError):
        make_settings(
            app_env="production",
            enable_dev_endpoints=True,
            lab_traffic_secret=GOOD_SECRET,
            gateway_bind="0.0.0.0",
        )
