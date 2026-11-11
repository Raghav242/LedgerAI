import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import SessionDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", summary="Database connectivity check (kept for compatibility)")
def health(session: SessionDep) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("Health check: database unreachable")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "degraded", "database": "unavailable"},
        )
    return JSONResponse(content={"status": "ok", "database": "ok"})


@router.get("/health/live", summary="Liveness: the process is up (no dependency checks)")
def live(request: Request) -> JSONResponse:
    # Deliberately touches nothing external: a Redis or database outage must not make
    # the orchestrator kill otherwise healthy replicas.
    return JSONResponse(content={"status": "ok", "replica": request.app.state.settings.replica_id})


@router.get("/health/ready", summary="Readiness: PostgreSQL and Redis reachable")
def ready(request: Request, session: SessionDep) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except SQLAlchemyError:
        logger.warning("Readiness: database unreachable")
        checks["database"] = "unavailable"

    redis_client = request.app.state.redis
    if redis_client is None:
        checks["redis"] = "not_used"
    else:
        try:
            redis_client.ping()
            checks["redis"] = "ok"
        except RedisError:
            logger.warning("Readiness: redis unreachable")
            checks["redis"] = "unavailable"

    ok = all(value in ("ok", "not_used") for value in checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "ok" if ok else "degraded",
            "replica": request.app.state.settings.replica_id,
            **checks,
        },
    )
