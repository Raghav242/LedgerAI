"""Experiment Lab units: allow-list, parameter bounds, signed lab traffic, policy choice,
response classification. No database, no network."""

import uuid

import pytest
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request

from app.api.dependencies import get_current_principal
from app.api.rate_limit import RateLimits, build_lab_burst_policies
from app.core.principal import DEMO_PRINCIPAL, Principal
from app.lab.catalog import experiment_catalog
from app.lab.experiments import EXPERIMENTS
from app.lab.experiments.rate_limiting import per_replica_counterfactual, tokens_per_second
from app.lab.schemas import PARAMS_BY_EXPERIMENT, LabRunCreate
from app.lab.signing import LAB_TRAFFIC_HEADER, LabTrafficSigner, lab_scope
from app.lab.traffic import classify
from app.rate_limiting import InMemoryFixedWindowLimiter, RateLimitPolicy

RUN_CREATE = TypeAdapter(LabRunCreate)
SECRET = b"0123456789abcdef-test-secret"


# --- allow-list and parameter bounds ------------------------------------------------------


def test_catalog_registry_and_param_models_cover_exactly_the_same_experiments() -> None:
    catalog_ids = {e.id for e in experiment_catalog()}
    assert catalog_ids == set(PARAMS_BY_EXPERIMENT)
    # The API can run exactly the traffic-only experiments (lab four + the AI scenario)...
    safe = {e.id for e in experiment_catalog() if e.runner == "api"}
    assert safe == {"concurrency", "idempotency", "rate_limiting", "load_balancing"}
    ai = {e.id for e in experiment_catalog() if e.runner == "ai"}
    assert ai == {"anomaly_scenario"}
    assert safe | ai == set(EXPERIMENTS)
    # ...and the infrastructure experiments are listed for the UI but run elsewhere.
    infra = {e.id for e in experiment_catalog() if e.runner == "controller"}
    assert infra == {"redis_outage", "rabbitmq_outage", "worker_crash", "replica_failure"}
    assert not infra & set(EXPERIMENTS)


def test_catalog_param_specs_mirror_the_validation_bounds() -> None:
    concurrency = next(e for e in experiment_catalog() if e.id == "concurrency")
    specs = {p.name: p for p in concurrency.params}
    assert specs["requests"].minimum == 2
    assert specs["requests"].maximum == 200
    assert specs["requests"].default == 100
    assert specs["amount_minor"].unit == "cents"
    for definition in experiment_catalog():
        if definition.runner in ("api", "ai"):
            assert definition.risk == "safe_local"
            assert definition.available
        else:  # never startable through the API
            assert definition.risk == "infrastructure"
            assert not definition.available and definition.unavailable_reason
        for spec in definition.params:
            assert spec.minimum <= spec.default <= spec.maximum


def test_defaults_are_applied_when_params_are_omitted() -> None:
    run = RUN_CREATE.validate_python({"experiment": "concurrency"})
    assert run.params.requests == 100
    assert run.params.amount_minor == 1_000
    assert run.params.funding_minor == 10_000


@pytest.mark.parametrize(
    "payload",
    [
        {"experiment": "shell", "params": {}},
        {"experiment": "__import__('os').system('id')"},
        {"experiment": "concurrency", "command": "rm -rf /"},
        {"experiment": "concurrency", "params": {"requests": 201}},
        {"experiment": "concurrency", "params": {"requests": 1}},
        {"experiment": "concurrency", "params": {"requests": "100"}},
        {"experiment": "concurrency", "params": {"requests": 10.5}},
        {"experiment": "concurrency", "params": {"script": "scripts/demo_concurrency.py"}},
        {"experiment": "rate_limiting", "params": {"refill_wait_seconds": 60}},
        {"experiment": "rate_limiting", "params": {"burst_requests": 10_000}},
        {"experiment": "load_balancing", "params": {"concurrency": 0}},
        {"experiment": "load_balancing", "params": {"target": "http://example.com"}},
        {"experiment": "idempotency", "params": {"duplicates": 101}},
    ],
)
def test_unknown_experiments_extra_fields_and_out_of_range_values_are_rejected(
    payload: dict,
) -> None:
    with pytest.raises(ValidationError):
        RUN_CREATE.validate_python(payload)


# --- signed lab traffic ---------------------------------------------------------------------


def test_signed_token_round_trips_and_binds_run_and_mode() -> None:
    signer = LabTrafficSigner(SECRET)
    run_id = uuid.uuid4()
    token = signer.token(run_id, "burst")
    assert signer.verify(token) == (run_id, "burst")
    principal = signer.principal_for(token)
    assert principal == Principal(scope=lab_scope(run_id), lab_rate_mode="burst")
    assert len(principal.scope) <= 100  # fits the idempotency scope column


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace(".burst.", ".standard."),  # change the mode, keep the MAC
        lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),  # tamper with the MAC
        lambda t: "v2" + t[2:],
        lambda t: t.split(".", 1)[1],
        lambda t: "",
        lambda t: "x" * 500,
    ],
)
def test_tampered_or_malformed_tokens_are_ignored(mutate) -> None:  # type: ignore[no-untyped-def]
    signer = LabTrafficSigner(SECRET)
    token = mutate(signer.token(uuid.uuid4(), "burst"))
    assert signer.verify(token) is None
    assert signer.principal_for(token) is None


def test_token_from_another_secret_is_rejected() -> None:
    other = LabTrafficSigner(b"another-secret-that-is-long-enough")
    assert LabTrafficSigner(SECRET).verify(other.token(uuid.uuid4(), "standard")) is None


def _request(app_state: dict, headers: dict[str, str]) -> Request:
    class _App:
        state = type("State", (), app_state)()

    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "app": _App(),
    }
    return Request(scope)


def test_principal_is_demo_merchant_without_a_valid_token() -> None:
    signer = LabTrafficSigner(SECRET)
    assert get_current_principal(_request({"lab_signer": signer}, {})) == DEMO_PRINCIPAL
    forged = {LAB_TRAFFIC_HEADER: f"v1.{uuid.uuid4().hex}.burst.{'0' * 64}"}
    assert get_current_principal(_request({"lab_signer": signer}, forged)) == DEMO_PRINCIPAL


def test_lab_token_is_ignored_when_the_lab_is_disabled() -> None:
    token = LabTrafficSigner(SECRET).token(uuid.uuid4(), "burst")
    request = _request({}, {LAB_TRAFFIC_HEADER: token})  # no lab_signer on app.state
    assert get_current_principal(request) == DEMO_PRINCIPAL


def test_valid_token_yields_the_per_run_principal() -> None:
    signer = LabTrafficSigner(SECRET)
    run_id = uuid.uuid4()
    request = _request({"lab_signer": signer}, {LAB_TRAFFIC_HEADER: signer.token(run_id, "burst")})
    assert get_current_principal(request).scope == f"lab:{run_id.hex}"


# --- rate-limit policy selection ------------------------------------------------------------


def _limits() -> RateLimits:
    read = RateLimitPolicy("read", 100, 50, 1000, True)
    write = RateLimitPolicy("write", 20, 10, 1000, False)
    lab_read, lab_write = build_lab_burst_policies()
    return RateLimits(InMemoryFixedWindowLimiter(), read, write, lab_read, lab_write)


def test_normal_and_standard_lab_traffic_use_the_real_policies() -> None:
    limits = _limits()
    standard = Principal(scope="lab:x", lab_rate_mode="standard")
    for principal in (DEMO_PRINCIPAL, standard):
        assert limits.policy_for(principal, "write") is limits.write
        assert limits.policy_for(principal, "read") is limits.read


def test_burst_lab_traffic_uses_the_lab_policies_with_the_same_fail_mode() -> None:
    limits = _limits()
    burst = Principal(scope="lab:x", lab_rate_mode="burst")
    write = limits.policy_for(burst, "write")
    read = limits.policy_for(burst, "read")
    assert write.name == "lab-burst-write"
    assert write.fail_open is False  # financial writes still fail closed
    assert read.fail_open is True
    assert write.capacity >= 200  # never throttles a bounded lab burst


# --- response classification and rate-limit helpers -----------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "replayed", "method", "expected"),
    [
        (201, None, False, "POST", "committed"),
        (201, None, True, "POST", "replayed"),
        (200, None, False, "GET", "read_ok"),
        (409, "INSUFFICIENT_FUNDS", False, "POST", "insufficient_funds"),
        (409, "IDEMPOTENCY_REQUEST_IN_PROGRESS", False, "POST", "in_progress"),
        (429, "RATE_LIMITED", False, "POST", "rate_limited"),
        (429, None, False, "POST", "rate_limited"),
        (503, "CONCURRENCY_CONFLICT", False, "POST", "conflict"),
        (422, "IDEMPOTENCY_KEY_REUSED", False, "POST", "key_reused"),
        (422, "VALIDATION_ERROR", False, "POST", "rejected"),
        (502, "HTTP_502", False, "POST", "server_error"),
        (None, None, False, "POST", "transport_error"),
    ],
)
def test_classify(status, code, replayed, method, expected) -> None:  # type: ignore[no-untyped-def]
    assert classify(status, code, replayed, method) == expected


def test_per_replica_counters_admit_capacity_per_replica() -> None:
    policy = RateLimitPolicy("write", 10, 1, 1000)
    assert per_replica_counterfactual(["api-1", "api-2"] * 25, policy) == 20
    assert per_replica_counterfactual(["api-1"] * 50, policy) == 10
    assert tokens_per_second(RateLimitPolicy("write", 20, 10, 1000)) == 10


@pytest.mark.parametrize(
    "experiment", ["redis_outage", "rabbitmq_outage", "worker_crash", "replica_failure"]
)
def test_the_api_refuses_to_start_infrastructure_experiments(experiment: str) -> None:
    with pytest.raises(ValidationError):
        RUN_CREATE.validate_python({"experiment": experiment})


def test_anomaly_summary_states_the_models_own_comparison_under_the_hybrid() -> None:
    """Phase 6B: a hybrid verdict can come from the rules while the model's score is below
    its threshold; the summary must not claim score >= threshold then."""
    from app.lab.experiments.anomaly import _summary

    rules_only = {"is_anomaly": True, "score": 0.51, "threshold": 0.998, "rule_flags": ["< 1 s"]}
    text = _summary("rapid_burst", rules_only, intent=True)
    assert text.startswith("The detector flagged this transfer")
    assert "model score 0.5100 < threshold 0.9980; rule baseline fired" in text
    both = {"is_anomaly": True, "score": 0.9991, "threshold": 0.998, "rule_flags": ["8x"]}
    assert "0.9991 ≥ threshold 0.9980" in _summary("large_amount", both, intent=True)
    neither = {"is_anomaly": False, "score": 0.4, "threshold": 0.998, "rule_flags": []}
    assert "did not flag" in _summary("normal", neither, intent=False)
    assert "rule baseline did not fire" in _summary("normal", neither, intent=False)
