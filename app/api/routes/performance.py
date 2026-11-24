"""Performance Lab API (Phase 5E): the RECORDED benchmark results, read-only.

Results are the JSON files the existing Locust harness writes (benchmarks/run_benchmarks.py
-> benchmarks/results/<timestamp>.json). The Compose replicas mount that directory
read-only. Nothing is measured here and nothing is invented: a missing directory or a
malformed file is reported as such.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.rate_limit import ReadRateLimit

router = APIRouter(prefix="/performance", tags=["performance"], dependencies=[ReadRateLimit])

RESULT_FILE = re.compile(r"^\d{8}-\d{6}\.json$")
MAX_FILES = 30
MAX_BYTES = 2_000_000


class BenchmarkResult(BaseModel):
    label: str
    replicas: int
    workload: str
    users: int
    duration_s: int
    requests: int
    rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    error_pct: float
    rejected_429: int
    other_failures: int
    pg_connections_max: int | None = None
    pg_connections_mean: float | None = None
    cpu_percent: dict[str, float] = {}
    transfer_rps: float | None = None
    transfers_admitted: int | None = None
    notes: list[str] = []
    # Phase 6C (absent in older result files): memory, contention and variability over
    # repeated runs. A summary row's rps/p50/p95/p99 are means over `repeats` runs.
    mem_mib: dict[str, float] = {}
    lock_timeouts: int = 0
    run: int | None = None
    repeats: int | None = None
    rps_std: float | None = None
    rps_min: float | None = None
    rps_max: float | None = None
    p95_std: float | None = None
    p99_std: float | None = None
    scaling_efficiency: float | None = None


class BenchmarkRun(BaseModel):
    id: str
    started_at: datetime | None
    meta: dict[str, Any]
    results: list[BenchmarkResult]
    runs: list[BenchmarkResult] = []  # every repeat behind the summary rows (Phase 6C)
    error: str | None = None


class BenchmarkList(BaseModel):
    directory_found: bool
    runs: list[BenchmarkRun]


def _started(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def read_runs(directory: Path) -> BenchmarkList:
    if not directory.is_dir():
        return BenchmarkList(directory_found=False, runs=[])
    files = sorted(
        (p for p in directory.iterdir() if p.is_file() and RESULT_FILE.match(p.name)),
        reverse=True,
    )[:MAX_FILES]
    runs = []
    for path in files:
        run_id = path.stem
        try:
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("file too large")
            payload = json.loads(path.read_text(encoding="utf-8"))
            runs.append(
                BenchmarkRun(
                    id=run_id,
                    started_at=_started(run_id),
                    meta=payload.get("meta", {}),
                    results=[BenchmarkResult(**r) for r in payload.get("results", [])],
                    runs=[BenchmarkResult(**r) for r in payload.get("runs", [])],
                )
            )
        except (OSError, ValueError, TypeError) as exc:
            runs.append(
                BenchmarkRun(
                    id=run_id,
                    started_at=_started(run_id),
                    meta={},
                    results=[],
                    error=f"unreadable: {type(exc).__name__}",
                )
            )
    return BenchmarkList(directory_found=True, runs=runs)


@router.get("/benchmarks", summary="Recorded Locust benchmark runs, newest first")
def list_benchmarks(request: Request) -> BenchmarkList:
    return read_runs(Path(request.app.state.settings.benchmark_results_dir))
