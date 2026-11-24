"""Benchmark runs started by the local controller (Phase 5E Performance Lab).

Runs the existing harness: benchmarks/run_benchmarks.py brings up the ISOLATED Compose
project `ledgerflow-bench` (own containers, network, volumes, ports and database), runs
Locust in a container on that project's network, writes
benchmarks/results/<timestamp>.{json,md} and removes the benchmark project in a `finally`.
The development stack is never redeployed. The controller only adds bounds, exclusivity
(results stay comparable when nothing else loads the machine) and progress reporting.
"""

import subprocess
import sys
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from labctl.infra import ROOT

HARNESS = ROOT / "benchmarks" / "run_benchmarks.py"
LOG_LINES = 200
RESTORE_SNIPPET = (
    "import sys; sys.path.insert(0, 'scripts'); import stack_control; "
    "stack_control.teardown(stack_control.BENCH)"
)


class BenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration_s: int = Field(default=20, ge=10, le=60, strict=True)
    users: int = Field(default=32, ge=8, le=64, strict=True)
    replicas: list[int] = Field(default=[1, 2], min_length=1, max_length=3)
    include_quota_run: bool = False
    confirm: Literal[True]

    @field_validator("replicas")
    @classmethod
    def _replicas(cls, value: list[int]) -> list[int]:
        if any(r not in (1, 2, 4) for r in value) or len(set(value)) != len(value):
            raise ValueError("replicas must be distinct values from {1, 2, 4}")
        return sorted(value)

    def command(self) -> list[str]:
        cmd = [
            sys.executable,
            str(HARNESS),
            "--duration",
            str(self.duration_s),
            "--users",
            str(self.users),
            "--replicas",
            *(str(r) for r in self.replicas),
            "--source",
            "controller",
        ]
        if not self.include_quota_run:
            cmd.append("--skip-quota-run")
        return cmd

    def time_limit_s(self) -> int:
        runs = len(self.replicas) * 2 + (1 if self.include_quota_run else 0)
        return runs * (self.duration_s + 60) + 600  # Locust windows + redeploys + restore


class BenchmarkStatus(BaseModel):
    state: Literal["idle", "running", "succeeded", "failed"]
    request: dict | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    result_file: str | None = None
    log_tail: list[str] = []


class BenchmarkJob:
    """At most one harness subprocess, with a bounded lifetime and captured output."""

    def __init__(self, popen=subprocess.Popen) -> None:  # type: ignore[no-untyped-def]
        self._popen = popen
        self._lock = threading.Lock()
        self._log: deque[str] = deque(maxlen=LOG_LINES)
        self._status = BenchmarkStatus(state="idle")
        self._process: subprocess.Popen[str] | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._status.state == "running"

    def status(self) -> BenchmarkStatus:
        with self._lock:
            return self._status.model_copy(update={"log_tail": list(self._log)[-40:]})

    def start(self, request: BenchmarkRequest) -> BenchmarkStatus:
        with self._lock:
            if self._status.state == "running":
                raise RuntimeError("a benchmark is already running")
            self._log.clear()
            self._process = self._popen(
                request.command(),
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._status = BenchmarkStatus(
                state="running", request=request.model_dump(), started_at=datetime.now(UTC)
            )
        threading.Thread(target=self._follow, args=(request.time_limit_s(),), daemon=True).start()
        return self.status()

    def _follow(self, limit_s: int) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        timer = threading.Timer(limit_s, process.kill)
        timer.start()
        result_file = None
        for line in process.stdout:
            line = line.rstrip()
            self._log.append(line)
            if line.startswith("Wrote "):
                result_file = line.removeprefix("Wrote ").strip()
        code = process.wait()
        timed_out = not timer.is_alive() and code != 0
        timer.cancel()
        if timed_out:
            # A killed harness never reaches its own `finally: teardown(BENCH)`.
            self._log.append(
                f"== time limit of {limit_s} s reached; removing the benchmark project"
            )
            restore = subprocess.run(
                [sys.executable, "-c", RESTORE_SNIPPET],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self._log.append(f"== restore exit code {restore.returncode}")
        with self._lock:
            self._status = self._status.model_copy(
                update={
                    "state": "succeeded" if code == 0 else "failed",
                    "finished_at": datetime.now(UTC),
                    "exit_code": code,
                    "result_file": result_file,
                }
            )
