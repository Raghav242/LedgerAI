"""Collects a run's timeline and progress in memory and flushes them to the store.

Lives inside the runner's event loop. Flushing happens on a timer (and at the end) in a
worker thread, so database writes never stall the experiment's in-flight requests.
The number of stored events is bounded; beyond the cap only counters are kept.
"""

import asyncio
import time
import uuid
from typing import Any

from app.lab.schemas import EventKind, EventLevel, LabCheck
from app.lab.store import LabRunStore
from app.lab.traffic import Observation

FLUSH_INTERVAL_SECONDS = 0.3
MAX_STORED_EVENTS = 800


class RunRecorder:
    def __init__(self, store: LabRunStore, run_id: uuid.UUID) -> None:
        self._store = store
        self._run_id = run_id
        self._started = time.monotonic()
        self._seq = 0
        self._pending: list[tuple[int, dict[str, Any]]] = []
        self._dropped = 0
        self.progress: dict[str, Any] = {
            "phase": "starting",
            "message": "Starting",
            "completed": 0,
            "total": 0,
        }

    # --- events ---------------------------------------------------------------------------

    def _emit(
        self,
        kind: EventKind,
        title: str,
        *,
        level: EventLevel = "info",
        detail: str | None = None,
        phase: str | None = None,
        request: dict[str, Any] | None = None,
    ) -> None:
        # Phase/check/note events are never dropped: only bulk request events are capped.
        if kind == "request" and self._seq >= MAX_STORED_EVENTS:
            self._dropped += 1
            return
        self._seq += 1
        payload: dict[str, Any] = {
            "t_ms": int((time.monotonic() - self._started) * 1000),
            "kind": kind,
            "level": level,
            "title": title,
            "detail": detail,
            "phase": phase or self.progress["phase"],
            "request": request,
        }
        self._pending.append((self._seq, payload))

    def phase(self, phase: str, title: str, *, detail: str | None = None, total: int = 0) -> None:
        self.progress = {"phase": phase, "message": title, "completed": 0, "total": total}
        self._emit("phase", title, detail=detail, phase=phase)

    def note(self, title: str, *, level: EventLevel = "info", detail: str | None = None) -> None:
        self._emit("note", title, level=level, detail=detail)

    def request(self, observation: Observation, *, final: bool = True) -> None:
        level: EventLevel = {
            "committed": "success",
            "replayed": "info",
            "read_ok": "success",
            "insufficient_funds": "warning",
            "rate_limited": "warning",
            "in_progress": "warning",
            "conflict": "warning",
            "key_reused": "warning",
        }.get(observation.outcome, "error")
        status = observation.status if observation.status is not None else "no response"
        title = f"#{observation.index + 1} {observation.method} → {status}"
        if observation.code:
            title += f" {observation.code}"
        self._emit("request", title, level=level, request=observation.event_payload())
        if final:
            self.progress["completed"] = self.progress.get("completed", 0) + 1

    def check(self, check: LabCheck) -> None:
        level: EventLevel = {"pass": "success", "fail": "error", "skipped": "info"}[check.status]
        self._emit("check", check.label, level=level, detail=check.observed, phase="verify")

    # --- persistence -------------------------------------------------------------------

    async def flush(self) -> None:
        events, self._pending = self._pending, []
        try:
            await asyncio.to_thread(
                self._store.flush,
                self._run_id,
                events=events,
                progress=dict(self.progress),
                event_count=self._seq,
                events_dropped=self._dropped,
            )
        except Exception:
            self._pending = events + self._pending  # retry on the next tick
            raise

    async def flush_forever(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # transient DB hiccup: keep the run going, retry next tick
                continue
