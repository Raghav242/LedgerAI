"""Experiment Lab API (Phase 5B). Development-only: mounted with ENABLE_DEV_ENDPOINTS.

Small, allow-listed job system. A client may only pick one of the catalog's experiment
ids and bounded integer parameters; it can never name a command, host, file or script.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query, Request, status

from app.api.rate_limit import ReadRateLimit, WriteRateLimit
from app.lab.catalog import experiment_catalog
from app.lab.runner import LabRunner
from app.lab.schemas import (
    ExperimentDefinition,
    LabEvent,
    LabEventPage,
    LabRunCreate,
    LabRunRead,
    LabRunSummary,
)
from app.models.lab_run import LabRun
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/lab", tags=["lab"])

MAX_EVENTS_PAGE = 500


def _runner(request: Request) -> LabRunner:
    return request.app.state.lab_runner


def _read(run: LabRun) -> LabRunRead:
    return LabRunRead(
        id=str(run.id),
        experiment=run.experiment,  # type: ignore[arg-type]
        status=run.status,  # type: ignore[arg-type]
        params=run.params,
        progress=run.progress,  # type: ignore[arg-type]
        result=run.result,  # type: ignore[arg-type]
        checks=run.checks,  # type: ignore[arg-type]
        error=run.error,
        runner_replica=run.runner_replica,
        target_base_url=run.target_base_url,
        event_count=run.event_count,
        events_dropped=run.events_dropped,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        heartbeat_at=run.heartbeat_at,
    )


def _summary(run: LabRun) -> LabRunSummary:
    checks = run.checks or []
    return LabRunSummary(
        id=str(run.id),
        experiment=run.experiment,  # type: ignore[arg-type]
        status=run.status,  # type: ignore[arg-type]
        params=run.params,
        summary=(run.result or {}).get("summary"),
        checks_passed=sum(1 for c in checks if c.get("status") == "pass"),
        checks_total=sum(1 for c in checks if c.get("status") != "skipped"),
        created_at=run.created_at,
        finished_at=run.finished_at,
    )


@router.get(
    "/experiments",
    summary="The allow-listed experiments with their bounded parameters",
    dependencies=[ReadRateLimit],
)
def list_experiments() -> list[ExperimentDefinition]:
    return experiment_catalog()


@router.get("/runs", summary="Recent experiment runs (newest first)", dependencies=[ReadRateLimit])
def list_runs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[LabRunSummary]:
    runner = _runner(request)
    runner.store.expire_stale_runs()
    return [_summary(run) for run in runner.store.list_recent(limit=limit)]


@router.post(
    "/runs",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an experiment run in the background",
    description=(
        "Validates the experiment id against the allow-list and every parameter against "
        "its bounds, then starts the run and returns immediately. Only one run may be "
        "active at a time (409 LAB_RUN_IN_PROGRESS)."
    ),
    responses={
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    },
    dependencies=[WriteRateLimit],
)
def start_run(request: Request, payload: Annotated[LabRunCreate, Body()]) -> LabRunRead:
    run = _runner(request).start(payload.experiment, payload.params)
    return _read(run)


@router.get(
    "/runs/{run_id}",
    summary="Status, progress, result and invariant checks of one run",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    dependencies=[ReadRateLimit],
)
def get_run(request: Request, run_id: uuid.UUID) -> LabRunRead:
    store = _runner(request).store
    run = store.get(run_id)
    if run.status in ("queued", "running"):
        store.expire_stale_runs()
        run = store.get(run_id)
    return _read(run)


@router.get(
    "/runs/{run_id}/events",
    summary="Timeline events of one run, incrementally (after a sequence number)",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    dependencies=[ReadRateLimit],
)
def get_run_events(
    request: Request,
    run_id: uuid.UUID,
    after: Annotated[int, Query(ge=0, description="Return events with seq > after")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENTS_PAGE)] = 200,
) -> LabEventPage:
    store = _runner(request).store
    run = store.get(run_id)
    events = store.events(run_id, after=after, limit=limit)
    return LabEventPage(
        items=[LabEvent(seq=e.seq, **e.payload) for e in events],
        next_after=events[-1].seq if events else after,
        run_status=run.status,  # type: ignore[arg-type]
        event_count=run.event_count,
        events_dropped=run.events_dropped,
    )
