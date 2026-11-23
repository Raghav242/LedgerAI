"""The local experiment controller: a small HTTP service on 127.0.0.1 only.

Why a separate process: stopping containers needs the Docker CLI, which must never be
reachable from the internet-facing API replicas (no Docker socket in containers, no
command endpoint in FastAPI). This process runs on the developer's machine, next to
`docker compose`, and can do exactly four allow-listed things (labctl/infra.py).

Layers of protection:
  - binds 127.0.0.1 only (not configurable); refuses to start when APP_ENV=production
    or when PostgreSQL / the API target are not on this machine
  - failure experiments are OFF unless started with --enable-failures
  - every request needs the per-start random token (written to .ledgerflow/, injected by
    the Vite dev proxy; browsers cannot read it), a loopback Host header (DNS-rebinding
    defence) and, if present, a local Origin; no CORS headers are ever sent, so a foreign
    web page cannot even complete a preflight
  - allow-listed experiment ids, bounded integer parameters, explicit confirmation
  - one run at a time (in-process lock AND the lab_runs one-active index)
  - every disturbance journaled first and restored afterwards; on startup a leftover
    journal (controller killed mid-run) is restored and its run marked as interrupted
"""

import asyncio
import hmac
import logging
import secrets
import threading
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from app.api.rate_limit import RateLimits, build_lab_burst_policies, build_policies
from app.core.config import Settings
from app.core.database import create_db_engine, create_session_factory
from app.lab.catalog import failure_definitions
from app.lab.errors import LabRunInProgressError
from app.lab.runner import LabRunner
from app.lab.schemas import (
    RabbitMqOutageParams,
    RedisOutageParams,
    ReplicaFailureParams,
    WorkerCrashParams,
)
from app.lab.signing import LabTrafficSigner
from app.models.lab_run import LAB_RUN_ACTIVE_STATUSES, LabRun
from app.rate_limiting import RedisTokenBucketLimiter, create_redis_client
from labctl.benchmarks import BenchmarkJob, BenchmarkRequest, BenchmarkStatus
from labctl.experiments import FAILURE_EXPERIMENTS, guarded
from labctl.infra import STATE_DIR, CommandRunner, Infra, Journal, RecoveryFailedError

logger = logging.getLogger("labctl")

HOST = "127.0.0.1"
DEFAULT_PORT = 8090
TARGET_BASE_URL = "http://127.0.0.1:8080"  # NGINX of the local stack, never configurable
TOKEN_HEADER = "x-ledgerflow-controller-token"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
RUN_TIMEOUT_SECONDS = 300
TOKEN_FILE = STATE_DIR / "controller-token"


# --- request contract ------------------------------------------------------------------------


class _Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True] = Field(
        description="Explicit confirmation that a local service may be stopped and restored."
    )


class RedisOutageCreate(_Create):
    experiment: Literal["redis_outage"]
    params: RedisOutageParams = RedisOutageParams()


class RabbitMqOutageCreate(_Create):
    experiment: Literal["rabbitmq_outage"]
    params: RabbitMqOutageParams = RabbitMqOutageParams()


class WorkerCrashCreate(_Create):
    experiment: Literal["worker_crash"]
    params: WorkerCrashParams = WorkerCrashParams()


class ReplicaFailureCreate(_Create):
    experiment: Literal["replica_failure"]
    params: ReplicaFailureParams = ReplicaFailureParams()


FailureRunCreate = Annotated[
    RedisOutageCreate | RabbitMqOutageCreate | WorkerCrashCreate | ReplicaFailureCreate,
    Field(discriminator="experiment"),
]


class ControllerStatus(BaseModel):
    enabled: bool
    failures_enabled: bool
    benchmarks_enabled: bool
    benchmark_running: bool
    busy: bool
    active_run_id: str | None
    experiments: list[str]
    interrupted_recovery: dict[str, Any] | None


# --- safety checks ---------------------------------------------------------------------------


def refuse_unsafe(settings: Settings) -> None:
    if settings.app_env == "production":
        raise SystemExit(
            "Refusing to start: the experiment controller is never allowed in production"
        )
    if settings.postgres_host not in LOCAL_HOSTS:
        raise SystemExit(
            f"Refusing to start: PostgreSQL at {settings.postgres_host!r} is not local"
        )


def write_token(path: Path = TOKEN_FILE) -> str:
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return token


def _host_ok(host_header: str | None) -> bool:
    host = (host_header or "").rsplit(":", 1)[0].strip("[]")
    return host in LOCAL_HOSTS


def _origin_ok(origin: str | None) -> bool:
    if origin is None:
        return True  # non-browser clients and same-origin GETs
    from urllib.parse import urlparse

    parsed = urlparse(origin)
    return parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS


# --- the app ------------------------------------------------------------------------------------


class Controller:
    def __init__(
        self,
        settings: Settings,
        *,
        failures_enabled: bool,
        token: str,
        benchmarks_enabled: bool = False,
        benchmark_job: BenchmarkJob | None = None,
        journal: Journal | None = None,
        command_runner: CommandRunner | None = None,
        experiments: dict | None = None,  # type: ignore[type-arg]
        run_timeout_seconds: int = RUN_TIMEOUT_SECONDS,
        base_url: str = TARGET_BASE_URL,
        transport_factory=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.settings = settings.model_copy(
            update={"replica_id": "lab-controller", "lab_target_base_url": base_url}
        )
        self.failures_enabled = failures_enabled
        self.token = token
        self.benchmarks_enabled = benchmarks_enabled
        self.benchmarks = benchmark_job or BenchmarkJob()
        self.journal = journal or Journal()
        self.command_runner = command_runner
        self.engine = create_db_engine(self.settings)
        self.sessions = create_session_factory(self.engine)
        self.redis_client = create_redis_client(self.settings)
        read, write = build_policies(self.settings)
        lab_read, lab_write = build_lab_burst_policies()
        rate_limits = RateLimits(
            limiter=RedisTokenBucketLimiter(
                self.redis_client, key_prefix=self.settings.rate_limit_key_prefix
            ),
            read=read,
            write=write,
            lab_burst_read=lab_read,
            lab_burst_write=lab_write,
        )
        registry = experiments or FAILURE_EXPERIMENTS
        self.runner = LabRunner(
            settings=self.settings,
            session_factory=self.sessions,
            rate_limits=rate_limits,
            signer=LabTrafficSigner.from_settings(settings),
            transport_factory=transport_factory,
            experiments={
                name: guarded(
                    fn,
                    journal=self.journal,
                    sessions=self.sessions,
                    base_url=base_url,
                    runner=command_runner,
                )
                for name, fn in registry.items()
            },
            run_timeout_seconds=run_timeout_seconds,
        )
        self._lock = threading.Lock()
        self.interrupted_recovery: dict[str, Any] | None = None

    def recover_interrupted(self) -> None:
        """A journal left behind means a run was interrupted mid-disturbance: restore now."""
        state = self.journal.load()
        if not state:
            return
        run_id = state.get("run_id", "unknown")
        logger.warning("Found an interrupted run %s; restoring %s", run_id, state.get("disturbed"))
        infra = (
            Infra(run_id, self.journal, self.command_runner)
            if self.command_runner
            else Infra(run_id, self.journal)
        )
        try:
            restored = asyncio.run(infra.restore())
            message = (
                f"The controller stopped mid-run; on restart it restored: {', '.join(restored)}."
            )
            outcome = {"run_id": run_id, "restored": restored, "ok": True}
        except RecoveryFailedError as exc:
            message = str(exc)
            outcome = {"run_id": run_id, "restored": [], "ok": False, "error": message}
        with self.sessions() as session, session.begin():
            session.execute(
                update(LabRun)
                .where(LabRun.id == run_id, LabRun.status.in_(LAB_RUN_ACTIVE_STATUSES))
                .values(status="error", error=message)
            )
        self.interrupted_recovery = outcome

    def status(self) -> ControllerStatus:
        active = self.runner.busy
        return ControllerStatus(
            enabled=True,
            failures_enabled=self.failures_enabled,
            benchmarks_enabled=self.benchmarks_enabled,
            benchmark_running=self.benchmarks.running,
            busy=active or self.benchmarks.running,
            active_run_id=None,
            experiments=sorted(self.runner.experiments) if self.failures_enabled else [],
            interrupted_recovery=self.interrupted_recovery,
        )

    def start(self, payload: Any) -> LabRun:
        if not self.failures_enabled:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Failure experiments are disabled. Start the controller with --enable-failures.",
            )
        if not self._lock.acquire(blocking=False):
            raise HTTPException(status.HTTP_409_CONFLICT, "The controller is starting another run.")
        try:
            if self.runner.busy or self.benchmarks.running:
                raise HTTPException(status.HTTP_409_CONFLICT, "The controller is already busy.")
            return self.runner.start(payload.experiment, payload.params)
        finally:
            self._lock.release()

    def _api_run_active(self) -> bool:
        with self.sessions() as session:
            active = session.scalar(
                select(func.count())
                .select_from(LabRun)
                .where(LabRun.status.in_(LAB_RUN_ACTIVE_STATUSES))
            )
        return (active or 0) > 0

    def start_benchmark(self, request: BenchmarkRequest) -> BenchmarkStatus:
        if not self.benchmarks_enabled:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Benchmarks are disabled. Start the controller with --enable-benchmarks.",
            )
        if not self._lock.acquire(blocking=False):
            raise HTTPException(status.HTTP_409_CONFLICT, "The controller is starting another run.")
        try:
            if self.runner.busy or self.benchmarks.running or self._api_run_active():
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Another experiment or benchmark is running; runs never overlap.",
                )
            return self.benchmarks.start(request)
        finally:
            self._lock.release()

    def close(self) -> None:
        self.runner.shutdown()
        self.engine.dispose()
        self.redis_client.close()


def create_controller_app(controller: Controller) -> FastAPI:
    app = FastAPI(
        title="LedgerFlow experiment controller (local only)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.controller = controller

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "OPTIONS":  # never answer CORS preflights
            return JSONResponse({"detail": "Not allowed"}, status_code=403)
        if not _host_ok(request.headers.get("host")):
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)
        if not _origin_ok(request.headers.get("origin")):
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        token = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(token.encode(), controller.token.encode()):
            return JSONResponse({"detail": "Missing or invalid controller token"}, status_code=401)
        return await call_next(request)

    @app.get("/controller/status")
    def get_status() -> ControllerStatus:
        return controller.status()

    @app.get("/controller/experiments")
    def get_experiments() -> list[dict[str, Any]]:
        return failure_definitions() if controller.failures_enabled else []

    @app.get("/controller/benchmarks/current")
    def benchmark_status() -> BenchmarkStatus:
        return controller.benchmarks.status()

    @app.post("/controller/benchmarks", status_code=status.HTTP_202_ACCEPTED)
    def start_benchmark(request: Annotated[BenchmarkRequest, Body()]) -> BenchmarkStatus:
        return controller.start_benchmark(request)

    @app.post("/controller/runs", status_code=status.HTTP_202_ACCEPTED)
    def start_run(payload: Annotated[FailureRunCreate, Body()]) -> dict[str, str]:
        try:
            run = controller.start(payload)
        except LabRunInProgressError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, exc.message) from exc
        return {"id": str(run.id), "experiment": run.experiment, "status": run.status}

    return app
