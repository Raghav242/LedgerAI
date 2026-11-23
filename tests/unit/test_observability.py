"""Observability units: replica probe classification, trace parsing, trace-context helpers."""

import httpx
import pytest

from app.core.config import Settings
from app.observability.topology import TopologyProbe
from app.observability.traces import fetch_trace
from app.observability.tracing import current_trace_parent, extract_context, trace_id_of

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def _settings(**overrides: object) -> Settings:
    base = Settings(postgres_user="u", postgres_password="p")  # type: ignore[arg-type]
    return base.model_copy(update=overrides)


def _probe(handler, **overrides: object) -> TopologyProbe:  # type: ignore[no-untyped-def]
    return TopologyProbe(
        settings=_settings(**{"observability_upstream_count": 2, **overrides}),
        session_factory=None,  # type: ignore[arg-type]  # not used by the replica probe
        redis_client=None,
        tracing_enabled=False,
        http_transport=httpx.MockTransport(handler),
    )


# --- replica readiness probe -------------------------------------------------------------


def test_ready_replica_is_healthy() -> None:
    probe = _probe(lambda r: httpx.Response(200, json={"database": "ok", "redis": "ok"}))
    health = probe._replica("api-1")
    assert (health.status, health.evidence) == ("healthy", "probe")
    assert health.last_ok_at is not None
    probe.close()


def test_replica_with_failed_dependency_is_degraded_not_healthy() -> None:
    probe = _probe(lambda r: httpx.Response(503, json={"database": "ok", "redis": "unavailable"}))
    health = probe._replica("api-2")
    assert health.status == "degraded"
    assert "redis" in health.message
    probe.close()


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("t"), httpx.ReadTimeout("t")]
)
def test_unreachable_replica_is_unhealthy_and_keeps_its_last_ok(exc: Exception) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"database": "ok", "redis": "ok"})
        raise exc

    probe = _probe(handler)
    ok = probe._replica("api-1")
    down = probe._replica("api-1")
    assert down.status == "unhealthy"
    assert down.last_error
    # Last OK is the earlier successful observation, not now.
    assert down.last_ok_at == ok.last_ok_at
    probe.close()


def test_replica_names_come_from_configuration_only() -> None:
    probe = _probe(lambda r: httpx.Response(200), observability_upstream_count=4)
    assert probe.replica_names == ["api-1", "api-2", "api-3", "api-4"]
    probe.close()


# --- Jaeger trace reading -------------------------------------------------------------------

JAEGER_TRACE = {
    "data": [
        {
            "traceID": TRACE,
            "spans": [
                {
                    "spanID": "b",
                    "operationName": "transfer.create",
                    "references": [{"refType": "CHILD_OF", "spanID": "a"}],
                    "startTime": 1_000_500,
                    "duration": 2_000,
                    "processID": "p1",
                    "tags": [
                        {"key": "ledgerflow.transfer.outcome", "value": "succeeded"},
                        {"key": "http.user_agent", "value": "secret-ish"},
                        {"key": "internal.other", "value": "dropped"},
                    ],
                },
                {
                    "spanID": "a",
                    "operationName": "POST /api/v1/transfers",
                    "references": [],
                    "startTime": 1_000_000,
                    "duration": 5_000,
                    "processID": "p1",
                    "tags": [{"key": "otel.status_code", "value": "ERROR"}],
                },
            ],
            "processes": {
                "p1": {
                    "serviceName": "ledgerflow-api",
                    "tags": [{"key": "service.instance.id", "value": "api-2"}],
                }
            },
        }
    ]
}


def _traced_settings() -> Settings:
    return _settings(otel_enabled=True, jaeger_query_url="http://jaeger:16686")


def test_trace_is_reduced_to_ordered_spans_with_whitelisted_attributes() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=JAEGER_TRACE)

    view = fetch_trace(_traced_settings(), TRACE.upper(), transport=httpx.MockTransport(handler))
    assert seen == [f"http://jaeger:16686/api/traces/{TRACE}"]
    assert view.available
    assert [s.name for s in view.spans] == ["POST /api/v1/transfers", "transfer.create"]
    root, child = view.spans
    assert (root.start_offset_ms, child.start_offset_ms, child.duration_ms) == (0, 0.5, 2.0)
    assert child.parent_id == "a" and child.instance == "api-2"
    assert child.attributes == {"ledgerflow.transfer.outcome": "succeeded"}
    assert root.error and not child.error


@pytest.mark.parametrize(("status", "reason"), [(404, "Not in the trace store"), (500, "HTTP 500")])
def test_missing_trace_is_reported_not_invented(status: int, reason: str) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json={}))
    view = fetch_trace(_traced_settings(), TRACE, transport=transport)
    assert not view.available and reason in (view.reason or "")
    assert view.spans == []


def test_trace_store_unreachable_or_tracing_off() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    view = fetch_trace(_traced_settings(), TRACE, transport=httpx.MockTransport(boom))
    assert not view.available and "not reachable" in (view.reason or "")
    off = fetch_trace(_settings(jaeger_query_url="http://jaeger:16686"), TRACE)
    assert not off.available and "Tracing is off" in (off.reason or "")


def test_invalid_trace_id_never_reaches_the_store() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    view = fetch_trace(_traced_settings(), "../../admin", transport=httpx.MockTransport(fail))
    assert not view.available


# --- trace-context helpers ------------------------------------------------------------------


def test_trace_id_of_parses_only_valid_traceparents() -> None:
    assert trace_id_of(f"00-{TRACE}-00f067aa0ba902b7-01") == TRACE
    assert trace_id_of("garbage") is None
    assert trace_id_of(None) is None


def test_no_trace_parent_outside_a_span() -> None:
    assert current_trace_parent() is None


def test_extract_context_reads_only_w3c_keys() -> None:
    ctx = extract_context({"traceparent": f"00-{TRACE}-00f067aa0ba902b7-01", "x-other": "1"})
    from opentelemetry import trace

    span_context = trace.get_current_span(ctx).get_span_context()
    assert format(span_context.trace_id, "032x") == TRACE
