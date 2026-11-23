"""Optional distributed tracing with OpenTelemetry (Phase 5C).

OFF by default (OTEL_ENABLED=false). Then nothing is exported and every helper here is a
cheap no-op: the OpenTelemetry API's default tracer records nothing and injects nothing.

When enabled (the `observability` Compose profile runs Jaeger), each process exports spans
over OTLP/HTTP:

  API replica   FastAPI server span -> rate_limit.check -> transfer.create
                  -> idempotency.claim -> ledger.post -> outbox.write, plus automatic
                  SQLAlchemy and Redis client spans
  publisher     outbox.publish (PRODUCER), parented by the request's trace context that
                was stored on the outbox row in the same COMMIT
  consumers     event.consume (CONSUMER), parented by the `traceparent` AMQP header

Tracing is observation only: no span ever changes control flow, and an unreachable
collector only drops spans in the background exporter.
"""

import logging
import re
from typing import TYPE_CHECKING, Any

from opentelemetry import context, propagate, trace

if TYPE_CHECKING:
    from fastapi import FastAPI
    from redis import Redis
    from sqlalchemy import Engine

    from app.core.config import Settings

logger = logging.getLogger(__name__)

tracer = trace.get_tracer("ledgerflow")

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
# High-frequency polling endpoints would flood the trace store with identical traces.
EXCLUDED_URLS = "health,/api/v1/observability,/api/v1/lab/runs/,/api/v1/ai/results"

_configured = False


def configure_tracing(settings: "Settings", *, service_name: str) -> bool:
    """Install an exporting tracer provider once per process. Returns True if tracing is on."""
    global _configured
    if not settings.otel_enabled:
        return False
    if _configured:
        return True
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.instance.id": settings.replica_id,
            "deployment.environment": settings.app_env,
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces", timeout=2
    )
    # Short delay so a trace is queryable within about a second of the request.
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=500))
    trace.set_tracer_provider(provider)
    _configured = True
    logger.info(
        "Tracing enabled: service=%s exporter=%s",
        service_name,
        settings.otel_exporter_otlp_endpoint,
    )
    return True


def instrument_api(app: "FastAPI", engine: "Engine", redis_client: "Redis | None") -> None:
    """Automatic server, SQL and Redis spans for an API replica (only if tracing is on)."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS)
    SQLAlchemyInstrumentor().instrument(engine=engine)
    if redis_client is not None and not RedisInstrumentor().is_instrumented_by_opentelemetry:
        RedisInstrumentor().instrument()


def instrument_worker_engine(engine: "Engine") -> None:
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine)


# --- context helpers -------------------------------------------------------------------


def current_trace_parent() -> str | None:
    """W3C traceparent of the active span, or None when nothing is being traced."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    value = carrier.get("traceparent")
    return value if value and _TRACEPARENT.match(value) else None


def current_trace_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    return format(span_context.trace_id, "032x") if span_context.is_valid else None


def trace_id_of(trace_parent: str | None) -> str | None:
    match = _TRACEPARENT.match(trace_parent or "")
    return match.group(1) if match else None


def extract_context(headers: dict[str, Any] | None) -> context.Context:
    """Parent context from message headers (only the W3C keys are read)."""
    carrier = {
        key: str(value)
        for key, value in (headers or {}).items()
        if key in ("traceparent", "tracestate") and value is not None
    }
    return propagate.extract(carrier)


def context_from_trace_parent(trace_parent: str | None) -> context.Context:
    return extract_context({"traceparent": trace_parent} if trace_parent else None)


def inject_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `headers` with the active span's trace context added."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {**headers, **carrier}
