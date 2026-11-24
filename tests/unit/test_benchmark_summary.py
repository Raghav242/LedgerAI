"""Phase 6C: benchmark summaries are plain statistics over measured repeats, nothing more."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def _harness():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "run_benchmarks_summary", ROOT / "benchmarks" / "run_benchmarks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(h, replicas: int, workload: str, rps: float, p95: float, n: int):  # type: ignore[no-untyped-def]
    r = h.Result(f"scale-{replicas}-{workload}", replicas, workload, 32, 20, run=n)
    r.rps, r.p50_ms, r.p95_ms, r.p99_ms, r.requests = rps, p95 / 2, p95, p95 * 2, int(rps * 20)
    r.cpu_percent, r.mem_mib = {"api-1": 50.0 + n}, {"api-1": 100.0}
    return r


def test_summary_is_mean_and_spread_of_the_repeats_with_scaling_efficiency() -> None:
    h = _harness()
    runs = [
        _run(h, 1, "ReadHeavyUser", 100, 50, 1),
        _run(h, 1, "ReadHeavyUser", 110, 60, 2),
        _run(h, 1, "ReadHeavyUser", 90, 40, 3),
        _run(h, 2, "ReadHeavyUser", 150, 40, 1),
        _run(h, 2, "ReadHeavyUser", 170, 40, 2),
    ]
    one, two = h.summarize(runs)
    assert (one.replicas, one.repeats, one.rps, one.rps_min, one.rps_max) == (1, 3, 100.0, 90, 110)
    assert one.rps_std == 10.0 and one.p95_ms == 50.0 and one.p95_std == 10.0
    assert one.cpu_percent == {"api-1": 52.0}  # mean of 51, 52, 53
    assert one.scaling_efficiency == 1.0
    assert two.rps == 160.0 and two.scaling_efficiency == 0.8  # 160 / (2 x 100)
    assert all(s.run is None and s.label.startswith("scale-") for s in (one, two))


def test_a_configuration_without_a_one_replica_baseline_gets_no_efficiency() -> None:
    h = _harness()
    (only,) = h.summarize([_run(h, 4, "HotAccountUser", 50, 900, 1)])
    assert only.scaling_efficiency is None and only.rps_std == 0.0


@pytest.mark.parametrize(("text", "mib"), [("512MiB", 512.0), ("1.5GiB", 1536.0), ("2048KiB", 2.0)])
def test_docker_memory_strings_parse_to_mib(text: str, mib: float) -> None:
    assert _harness()._mib(text) == mib


def test_recorded_results_are_readable_by_the_api(tmp_path: Path) -> None:
    from app.api.routes.performance import read_runs

    h = _harness()
    runs = [_run(h, 1, "MixedTransferUser", 100, 50, i) for i in (1, 2)]
    payload = {
        "meta": {"repeats": 2},
        "results": [h.asdict(s) for s in h.summarize(runs)],
        "runs": [h.asdict(r) for r in runs],
    }
    (tmp_path / "20261123-120000.json").write_text(json.dumps(payload))
    listing = read_runs(tmp_path)
    (run,) = listing.runs
    assert run.error is None and len(run.results) == 1 and len(run.runs) == 2
    assert run.results[0].repeats == 2 and run.results[0].rps_std == 0.0
