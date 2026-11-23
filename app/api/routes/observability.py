"""Observability API (Phase 5C): live topology, event pipeline, lifecycles and traces.

Strictly READ-ONLY: every route is a GET; nothing here starts, stops or changes anything.
Mounted with the development-only surface (ENABLE_DEV_ENDPOINTS) because it reveals
internal topology (replica names, error messages) that a public API should not.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request, status

from app.api.dependencies import SessionDep
from app.api.rate_limit import ReadRateLimit
from app.observability.pipeline import InvalidCursorError, PipelineReader, StatusFilter
from app.observability.schemas import (
    EventLifecycle,
    EventPage,
    PipelineSummary,
    TopologySnapshot,
    TraceView,
)
from app.observability.topology import TopologyProbe
from app.observability.traces import fetch_trace
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/observability", tags=["observability"], dependencies=[ReadRateLimit])

EventType = Literal["transfer.completed", "account.funded"]


def _parse_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not cursor:
        return None
    try:
        at, _, raw_id = cursor.partition("|")
        return datetime.fromisoformat(at), uuid.UUID(raw_id)
    except ValueError as exc:
        raise InvalidCursorError("Malformed cursor", {"cursor": cursor[:100]}) from exc


@router.get("/topology", summary="Live health of every component, with its evidence")
def topology(request: Request) -> TopologySnapshot:
    probe: TopologyProbe = request.app.state.topology_probe
    return probe.snapshot(request_id=request.headers.get("x-request-id"))


@router.get("/events/summary", summary="Outbox, publisher and consumer aggregates")
def pipeline_summary(request: Request, session: SessionDep) -> PipelineSummary:
    return PipelineReader(session, request.app.state.settings).summary()


@router.get("/events", summary="Outbox events, newest first (keyset pagination)")
def list_events(
    request: Request,
    session: SessionDep,
    status_filter: Annotated[StatusFilter, Query(alias="status")] = "all",
    event_type: EventType | None = None,
    correlation_id: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=120)] = None,
) -> EventPage:
    return PipelineReader(session, request.app.state.settings).events(
        status=status_filter,
        event_type=event_type,
        correlation_id=correlation_id,
        limit=limit,
        cursor=_parse_cursor(cursor),
    )


@router.get(
    "/events/{event_id}",
    summary="One event: transfer -> outbox -> publisher -> consumers",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
def event_lifecycle(request: Request, session: SessionDep, event_id: uuid.UUID) -> EventLifecycle:
    return PipelineReader(session, request.app.state.settings).lifecycle_for_event(event_id)


@router.get(
    "/transfers/{transfer_id}/lifecycle",
    summary="The persisted asynchronous path of one money movement",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
def transfer_lifecycle(
    request: Request, session: SessionDep, transfer_id: uuid.UUID
) -> EventLifecycle:
    return PipelineReader(session, request.app.state.settings).lifecycle_for_transfer(transfer_id)


@router.get("/traces/{trace_id}", summary="One distributed trace from the trace store, if any")
def get_trace(
    request: Request,
    trace_id: Annotated[str, Path(min_length=32, max_length=32, pattern=r"^[0-9a-fA-F]{32}$")],
) -> TraceView:
    return fetch_trace(request.app.state.settings, trace_id)
