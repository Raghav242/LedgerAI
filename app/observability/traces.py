"""Read one trace from Jaeger's query API and reduce it to what the UI shows (Phase 5C).

Read-only, bounded (one trace, at most MAX_SPANS spans, a whitelist of attributes), with a
short timeout. The trace id is validated as 32 hex characters before it is put in a URL.
If tracing is off, Jaeger is not configured, or the trace is not (yet) stored, the answer
says so explicitly: the UI then shows the persisted evidence only, never invented spans.
"""

import httpx

from app.core.config import Settings
from app.observability.schemas import TraceSpan, TraceView
from app.observability.tracing import TRACE_ID

MAX_SPANS = 300
TIMEOUT = httpx.Timeout(2.0, connect=0.5)
ATTRIBUTE_PREFIXES = ("ledgerflow.", "http.", "db.system", "db.operation", "messaging.", "url.")
SKIPPED_ATTRIBUTES = {"http.user_agent", "user_agent.original", "http.client_ip"}


def _attributes(tags: list[dict]) -> dict[str, str | int | float | bool]:
    out: dict[str, str | int | float | bool] = {}
    for tag in tags:
        key, value = tag.get("key", ""), tag.get("value")
        if key in SKIPPED_ATTRIBUTES or not key.startswith(ATTRIBUTE_PREFIXES):
            continue
        if isinstance(value, str | int | float | bool):
            out[key] = value if not isinstance(value, str) else value[:200]
    return out


def fetch_trace(
    settings: Settings, trace_id: str, *, transport: httpx.BaseTransport | None = None
) -> TraceView:
    trace_id = trace_id.lower()
    if not TRACE_ID.match(trace_id):
        return TraceView(trace_id=trace_id, available=False, reason="Not a valid trace id.")
    if settings.trace_store_url is None:
        return TraceView(
            trace_id=trace_id,
            available=False,
            reason="Tracing is off (OTEL_ENABLED=false), so no trace store is queried.",
        )
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            response = client.get(f"{settings.trace_store_url}/api/traces/{trace_id}")
    except httpx.HTTPError as exc:
        return TraceView(
            trace_id=trace_id,
            available=False,
            reason=f"The trace store is not reachable ({type(exc).__name__}).",
        )
    if response.status_code == 404:
        return TraceView(
            trace_id=trace_id,
            available=False,
            reason="Not in the trace store (not exported yet, sampled out, or expired).",
        )
    if response.status_code != 200:
        return TraceView(
            trace_id=trace_id,
            available=False,
            reason=f"The trace store answered HTTP {response.status_code}.",
        )
    data = (response.json().get("data") or [None])[0]
    if not data or not data.get("spans"):
        return TraceView(trace_id=trace_id, available=False, reason="The trace has no spans yet.")
    processes = data.get("processes", {})
    raw = sorted(data["spans"], key=lambda s: s.get("startTime", 0))
    origin = raw[0].get("startTime", 0)
    spans = []
    for span in raw[:MAX_SPANS]:
        process = processes.get(span.get("processID"), {})
        process_tags = {t.get("key"): t.get("value") for t in process.get("tags", [])}
        parent = next(
            (r.get("spanID") for r in span.get("references", []) if r.get("refType") == "CHILD_OF"),
            None,
        )
        tags = span.get("tags", [])
        error = any(
            (t.get("key") == "error" and t.get("value") in (True, "true"))
            or (t.get("key") == "otel.status_code" and t.get("value") == "ERROR")
            for t in tags
        )
        spans.append(
            TraceSpan(
                span_id=span.get("spanID", ""),
                parent_id=parent,
                name=span.get("operationName", "")[:120],
                service=process.get("serviceName", "unknown"),
                instance=process_tags.get("service.instance.id"),
                start_offset_ms=round((span.get("startTime", 0) - origin) / 1000, 3),
                duration_ms=round(span.get("duration", 0) / 1000, 3),
                error=error,
                attributes=_attributes(tags),
            )
        )
    return TraceView(
        trace_id=trace_id,
        available=True,
        spans=spans,
        services=sorted({s.service for s in spans}),
        truncated=len(raw) > MAX_SPANS,
    )
