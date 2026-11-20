"""Runs one allow-listed experiment in the background.

Execution model: the replica that accepts POST /lab/runs starts a daemon thread with its
own asyncio event loop. That keeps experiment traffic (hundreds of concurrent client
requests) off the API's request-serving loop and thread pool, and lets the HTTP call
return 202 immediately. Progress and events are flushed to PostgreSQL, where any replica
can serve them to the browser.

Bounded everywhere: validated parameters, at most one active run (database-enforced),
a per-request timeout, a per-run timeout, a cap on stored events. On shutdown the active
run is cancelled and marked `error` instead of being left `running`.
"""

import asyncio
import logging
import threading
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from app.api.rate_limit import RateLimits
from app.core.config import Settings
from app.lab.context import ExperimentContext, TransportFactory
from app.lab.experiments import EXPERIMENTS, RUN_TIMEOUT_SECONDS
from app.lab.recorder import RunRecorder
from app.lab.schemas import LabRunResult
from app.lab.signing import LabTrafficSigner
from app.lab.store import LabRunStore
from app.models.lab_run import LabRun

logger = logging.getLogger(__name__)


class LabRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        rate_limits: RateLimits,
        signer: LabTrafficSigner,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self.settings = settings
        self.store = LabRunStore(session_factory)
        self._session_factory = session_factory
        self._rate_limits = rate_limits
        self._signer = signer
        # Tests inject an in-process ASGI transport; production uses real HTTP.
        self.transport_factory = transport_factory
        self._lock = threading.Lock()
        self._active: tuple[asyncio.AbstractEventLoop, asyncio.Task[None]] | None = None
        self._threads: list[threading.Thread] = []

    def start(self, experiment: str, params: BaseModel) -> LabRun:
        if experiment not in EXPERIMENTS:  # unreachable through the API schema; defence in depth
            raise ValueError(f"unknown experiment {experiment!r}")
        run = self.store.create(
            experiment=experiment,
            params=params.model_dump(),
            runner_replica=self.settings.replica_id,
            target_base_url=self.settings.lab_target_base_url,
        )
        thread = threading.Thread(
            target=self._thread_main,
            args=(run.id, experiment, params),
            name=f"lab-run-{run.id.hex[:8]}",
            daemon=True,
        )
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()
        return run

    def _thread_main(self, run_id: uuid.UUID, experiment: str, params: BaseModel) -> None:
        asyncio.run(self._main(run_id, experiment, params))

    async def _main(self, run_id: uuid.UUID, experiment: str, params: BaseModel) -> None:
        task = asyncio.current_task()
        assert task is not None
        with self._lock:
            self._active = (asyncio.get_running_loop(), task)
        try:
            await self._execute(run_id, experiment, params)
        finally:
            with self._lock:
                self._active = None

    async def _execute(self, run_id: uuid.UUID, experiment: str, params: BaseModel) -> None:
        recorder = RunRecorder(self.store, run_id)
        ctx = ExperimentContext(
            run_id=run_id,
            recorder=recorder,
            session_factory=self._session_factory,
            settings=self.settings,
            rate_limits=self._rate_limits,
            signer=self._signer,
            transport_factory=self.transport_factory,
        )
        status = "error"
        result: dict[str, Any] | None = None
        checks: list[dict[str, Any]] | None = None
        error: str | None = None
        flusher: asyncio.Task[None] | None = None
        try:
            await asyncio.to_thread(self.store.mark_running, run_id)
            flusher = asyncio.create_task(recorder.flush_forever())
            recorder.note(
                f"Run started on {self.settings.replica_id}",
                detail=f"Measured traffic goes to {self.settings.lab_target_base_url}",
            )
            outcome = await asyncio.wait_for(
                EXPERIMENTS[experiment](ctx, params), timeout=RUN_TIMEOUT_SECONDS
            )
            for item in outcome.checks:
                recorder.check(item)
            failed = [c for c in outcome.checks if c.status == "fail"]
            status = "failed" if failed else "passed"
            checks = [c.model_dump() for c in outcome.checks]
            result = LabRunResult(
                summary=outcome.summary,
                explanation=outcome.explanation,
                metrics=outcome.metrics,
                outcome_counts=outcome.outcome_counts,
                data=outcome.data,
            ).model_dump(mode="json")
            recorder.progress = {**recorder.progress, "phase": "done", "message": "Finished"}
            recorder.note(
                "All invariants held" if not failed else f"{len(failed)} invariant(s) violated",
                level="success" if not failed else "error",
            )
        except TimeoutError:
            error = f"The run exceeded its {RUN_TIMEOUT_SECONDS} s limit and was stopped."
        except asyncio.CancelledError:
            error = "The run was cancelled because its replica is shutting down."
        except Exception as exc:
            logger.exception("Lab run %s (%s) failed", run_id, experiment)
            error = f"{type(exc).__name__}: {exc}"[:2000]
        finally:
            if error:
                recorder.progress = {**recorder.progress, "phase": "error", "message": error}
                recorder.note("Run could not complete", level="error", detail=error)
            if flusher is not None:
                flusher.cancel()
            await self._finish(recorder, run_id, status, result, checks, error)

    async def _finish(
        self,
        recorder: RunRecorder,
        run_id: uuid.UUID,
        status: str,
        result: dict[str, Any] | None,
        checks: list[dict[str, Any]] | None,
        error: str | None,
    ) -> None:
        try:
            await recorder.flush()
            await asyncio.to_thread(
                self.store.finish, run_id, status=status, result=result, checks=checks, error=error
            )
        except Exception:
            logger.exception("Could not record the end of lab run %s", run_id)

    # --- lifecycle ------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active is not None

    def shutdown(self, timeout: float = 10) -> None:
        """Cancel the active run (it is recorded as `error`) and wait for runner threads."""
        with self._lock:
            active = self._active
            threads = list(self._threads)
        if active is not None:
            loop, task = active
            loop.call_soon_threadsafe(task.cancel)
        for thread in threads:
            thread.join(timeout)

    def wait_idle(self, timeout: float = 60) -> bool:
        """Block until every started run thread has finished (tests and shutdown)."""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)
        return not any(t.is_alive() for t in threads)
