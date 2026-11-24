"""Benchmark 1 / 2 / 4 API replicas with Locust, plus a small-quota rate-limit run.

LOCAL ONLY. Runs against an ISOLATED Compose project, `ledgerflow-bench` (Phase 6A): the
same docker-compose.yml, but its own containers, network, volumes (its own PostgreSQL
server with database ledgerflow_bench), Redis and loopback ports (gateway 127.0.0.1:8180).
The development stack (`ledgerflow`, port 8080) is never redeployed, reconfigured or read,
so its data and any experiment running on it are unaffected. The benchmark project is
created fresh for every run and removed afterwards, volumes included.
Results: benchmarks/results/<timestamp>.{json,md}

Measurement hygiene:
- Accounts are created and funded BEFORE any measured run (funding locks the single
  system account; doing it inside the window would measure that hot row instead).
- Locust runs in a Linux container on the benchmark stack's internal network with several
  worker processes, so the load generator is not limited to one Python process and
  requests bypass Docker Desktop's host port forwarding.
- The benchmark stack has no broker or event workers (see stack_control.BENCH); results
  recorded before Phase 6A had them running alongside the replicas on the same machine.

    python benchmarks/run_benchmarks.py [--duration 30] [--users 64] [--replicas 1 2 4]
        [--repeats 3] [--warmup 10] [--workloads ReadHeavyUser MixedTransferUser HotAccountUser]

Repeatability (Phase 6C): for every (replicas, workload) the harness first runs an
unmeasured WARMUP (connection pools, JIT-free Python caches, PostgreSQL buffers), then
REPEATS identical measured runs. `results` holds one row per configuration whose numbers
are the MEAN over the repeats, with their standard deviation, min and max; every single
repeat is kept in `runs`. Scaling efficiency = mean req/s with N replicas / (N x mean req/s
with 1 replica). Nothing is extrapolated: a configuration that was not run is absent.
"""

import argparse
import contextlib
import csv
import json
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import stack_control  # noqa: E402

PROJECT = stack_control.BENCH
BENCH_DB = stack_control.BENCH_DB
LOCUSTFILE = ROOT / "benchmarks" / "locustfile.py"
RESULTS = ROOT / "benchmarks" / "results"
LOCUST_IMAGE = "locustio/locust:2.47.0"
LOCUST_PROCESSES = 4
INTERNAL_URL = "http://nginx:8080"
WORKLOADS = ("ReadHeavyUser", "MixedTransferUser", "HotAccountUser")
UNLIMITED = {
    f"RATE_LIMIT_{kind}_{name}": value
    for kind in ("READ", "WRITE")
    for name, value in (
        ("CAPACITY", "100000"),
        ("REFILL_TOKENS", "100000"),
        ("REFILL_INTERVAL_MS", "1000"),
    )
}
BENCH_ENV = {
    "ENABLE_DEV_ENDPOINTS": "true",
    "EXPOSE_REPLICA_HEADER": "true",
    "LOG_LEVEL": "WARNING",  # per-transfer INFO logs would be part of what we measure
    "API_DB_POOL_SIZE": "5",
    "API_DB_MAX_OVERFLOW": "5",
    **UNLIMITED,
}
SMALL_QUOTA = {
    **BENCH_ENV,
    "RATE_LIMIT_WRITE_CAPACITY": "50",
    "RATE_LIMIT_WRITE_REFILL_TOKENS": "20",
    "RATE_LIMIT_WRITE_REFILL_INTERVAL_MS": "1000",
}


@dataclass
class Result:
    label: str
    replicas: int
    workload: str
    users: int
    duration_s: int
    requests: int = 0
    rps: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    error_pct: float = 0.0
    rejected_429: int = 0
    other_failures: int = 0
    pg_connections_max: int = 0
    pg_connections_mean: float = 0.0
    cpu_percent: dict[str, float] = field(default_factory=dict)  # mid-run docker stats
    mem_mib: dict[str, float] = field(default_factory=dict)  # mid-run docker stats
    transfer_rps: float | None = None  # all POST /transfers attempts per second
    transfers_admitted: int | None = None  # POST /transfers that were not 429
    lock_timeouts: int = 0  # 503 CONCURRENCY_CONFLICT (hot-account contention; retryable)
    notes: list[str] = field(default_factory=list)
    run: int | None = None  # repeat number (1-based) for single runs; None for summaries
    # Summaries only (Phase 6C): variability over the repeats.
    repeats: int | None = None
    rps_std: float | None = None
    rps_min: float | None = None
    rps_max: float | None = None
    p95_std: float | None = None
    p99_std: float | None = None
    scaling_efficiency: float | None = None


def configure(replicas: int, env: dict[str, str]) -> None:
    stack_control.remove_extra_replicas(PROJECT)
    profiles = ("scale4",) if replicas == 4 else ()
    services = PROJECT.services + (("api-3", "api-4") if replicas == 4 else ())
    # NGINX must list exactly the deployed replicas (see deploy/nginx/nginx.conf).
    env = {**env, "NGINX_UPSTREAMS": "4" if replicas == 4 else "2"}
    stack_control.deploy(env, profiles=profiles, project=PROJECT, services=services)
    if replicas == 1:
        stack_control.compose("stop", "api-2", project=PROJECT)
    expected = {f"api-{i}" for i in range(1, replicas + 1)}
    stack_control.wait_until_served_only_by(PROJECT.base_url, expected, probes=16)


class ConnectionSampler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.samples: list[int] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(subprocess.CalledProcessError):
                self.samples.append(
                    stack_control.postgres_client_connections(BENCH_DB, project=PROJECT)
                )
            self._stop.wait(1)

    def stop(self) -> None:
        self._stop.set()
        self.join()


def _mib(text: str) -> float:
    """'123.4MiB' / '1.2GiB' / '900KiB' -> MiB."""
    value = text.strip()
    for unit, factor in (("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024), ("B", 1 / 1048576)):
        if value.endswith(unit):
            return round(float(value[: -len(unit)]) * factor, 1)
    return 0.0


def sample_cpu(result: Result, delay_s: float) -> threading.Thread:
    """One `docker stats` snapshot half-way through the run (CPU: 100% = one core)."""

    def take() -> None:
        time.sleep(delay_s)
        out = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        prefix = f"{PROJECT.container_prefix}-"
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            name, cpu, mem = parts
            # Only the benchmark project's containers (never the development stack's).
            if name.startswith(prefix) and cpu.endswith("%"):
                short = name.removeprefix(prefix).removesuffix("-1")
                result.cpu_percent[short] = float(cpu.rstrip("%"))
                result.mem_mib[short] = _mib(mem.split("/")[0])

    thread = threading.Thread(target=take, daemon=True)
    thread.start()
    return thread


def provision_accounts(pairs: int, path: Path) -> None:
    """Create and fund `pairs` account pairs through the API (unlimited quota)."""
    import httpx

    accounts: list[list[str]] = []
    with httpx.Client(base_url=PROJECT.base_url, timeout=30) as client:
        for i in range(pairs):
            pair = []
            for side in ("a", "b"):
                created = client.post("/api/v1/accounts", json={"name": f"bench-{i}-{side}"})
                created.raise_for_status()
                account_id = created.json()["id"]
                client.post(
                    f"/api/v1/accounts/{account_id}/fund", json={"amount_minor": 10_000_000}
                ).raise_for_status()
                pair.append(account_id)
            accounts.append(pair)
    path.write_text(json.dumps(accounts))


def locust_command(
    workload: str, users: int, duration_s: int, accounts_in_container: str, csv: str | None
) -> list[str]:
    cmd = [
        "-f",
        "/mnt/bench/locustfile.py",
        workload,
        "--headless",
        "-u",
        str(users),
        "-r",
        str(users),
        "-t",
        f"{duration_s}s",
        "--host",
        INTERNAL_URL,
        "--processes",
        str(LOCUST_PROCESSES),
        "--only-summary",
        "--loglevel",
        "WARNING",
        "--exit-code-on-error",
        "0",
    ]
    return [*cmd, "--csv", csv, "--reset-stats"] if csv else cmd


def warmup(workload: str, users: int, duration_s: int, accounts_file: Path) -> None:
    """An unmeasured run of the same workload: fills pools and caches before measuring."""
    work = accounts_file.parent
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            PROJECT.network,
            "-v",
            f"{(ROOT / 'benchmarks').as_posix()}:/mnt/bench:ro",
            "-v",
            f"{work.as_posix()}:/mnt/out",
            "-e",
            f"LEDGERFLOW_BENCH_ACCOUNTS=/mnt/out/{accounts_file.name}",
            "-e",
            f"LEDGERFLOW_USERS_PER_PROCESS={-(-users // LOCUST_PROCESSES)}",
            LOCUST_IMAGE,
            *locust_command(workload, users, duration_s, "", None),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def run_locust(result: Result, csv_prefix: Path, accounts_file: Path) -> None:
    sampler = ConnectionSampler()
    sampler.start()
    cpu = sample_cpu(result, result.duration_s / 2)
    # Locustfile read-only from the repository; this run's work directory (CSV output and
    # the provisioned accounts) mounted separately, so --results-dir may be anywhere.
    work = csv_prefix.parent
    mount = f"{(ROOT / 'benchmarks').as_posix()}:/mnt/bench:ro"
    work_mount = f"{work.as_posix()}:/mnt/out"
    in_container = f"/mnt/out/{csv_prefix.name}"
    accounts_in_container = f"/mnt/out/{accounts_file.relative_to(work).as_posix()}"
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            PROJECT.network,
            "-v",
            mount,
            "-v",
            work_mount,
            "-e",
            f"LEDGERFLOW_BENCH_ACCOUNTS={accounts_in_container}",
            "-e",
            f"LEDGERFLOW_USERS_PER_PROCESS={-(-result.users // LOCUST_PROCESSES)}",
            LOCUST_IMAGE,
            *locust_command(
                result.workload,
                result.users,
                result.duration_s,
                accounts_in_container,
                in_container,
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    sampler.stop()
    cpu.join()
    if proc.returncode != 0:
        raise RuntimeError(f"locust failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    if "CPU usage above" in proc.stderr:
        result.notes.append("Locust reported high load-generator CPU usage")

    with open(f"{csv_prefix}_stats.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    total = next(r for r in rows if r["Name"] == "Aggregated")
    result.requests = int(total["Request Count"])
    result.rps = float(total["Requests/s"])
    result.p50_ms = float(total["50%"])
    result.p95_ms = float(total["95%"])
    result.p99_ms = float(total["99%"])
    failures = int(total["Failure Count"])
    result.error_pct = round(100 * failures / max(result.requests, 1), 3)
    transfers = next((r for r in rows if r["Name"].startswith("POST /transfers")), None)
    transfer_429s = 0

    with open(f"{csv_prefix}_failures.csv", newline="") as f:
        for row in csv.DictReader(f):
            if "429" in row["Error"]:
                result.rejected_429 += int(row["Occurrences"])
                if row["Name"].startswith("POST /transfers"):
                    transfer_429s += int(row["Occurrences"])
            elif "503 lock timeout" in row["Error"]:
                result.lock_timeouts += int(row["Occurrences"])
            else:
                result.other_failures += int(row["Occurrences"])
    if transfers is not None:
        result.transfer_rps = float(transfers["Requests/s"])
        result.transfers_admitted = int(transfers["Request Count"]) - transfer_429s
    if sampler.samples:
        result.pg_connections_max = max(sampler.samples)
        result.pg_connections_mean = round(statistics.mean(sampler.samples), 1)


def summarize(runs: list[Result]) -> list[Result]:
    """One row per (replicas, workload): means over the repeats plus their spread."""
    groups: dict[tuple[int, str], list[Result]] = defaultdict(list)
    for r in runs:
        groups[(r.replicas, r.workload)].append(r)
    out: list[Result] = []
    for (replicas, workload), rs in sorted(groups.items()):
        rps = [r.rps for r in rs]

        def mean(values: list[float]) -> float:
            return round(statistics.mean(values), 2)

        def std(values: list[float]) -> float:
            return round(statistics.stdev(values), 2) if len(values) > 1 else 0.0

        containers = {k for r in rs for k in r.cpu_percent}
        summary = Result(
            label=f"scale-{replicas}-{workload}",
            replicas=replicas,
            workload=workload,
            users=rs[0].users,
            duration_s=rs[0].duration_s,
            requests=round(statistics.mean(r.requests for r in rs)),
            rps=mean(rps),
            p50_ms=mean([r.p50_ms for r in rs]),
            p95_ms=mean([r.p95_ms for r in rs]),
            p99_ms=mean([r.p99_ms for r in rs]),
            error_pct=round(statistics.mean(r.error_pct for r in rs), 3),
            rejected_429=sum(r.rejected_429 for r in rs),
            other_failures=sum(r.other_failures for r in rs),
            lock_timeouts=sum(r.lock_timeouts for r in rs),
            pg_connections_max=max(r.pg_connections_max for r in rs),
            pg_connections_mean=mean([r.pg_connections_mean for r in rs]),
            cpu_percent={
                c: mean([r.cpu_percent[c] for r in rs if c in r.cpu_percent]) for c in containers
            },
            mem_mib={c: mean([r.mem_mib[c] for r in rs if c in r.mem_mib]) for c in containers},
            transfer_rps=(
                mean([r.transfer_rps for r in rs if r.transfer_rps is not None])
                if any(r.transfer_rps is not None for r in rs)
                else None
            ),
            notes=sorted({n for r in rs for n in r.notes}),
            repeats=len(rs),
            rps_std=std(rps),
            rps_min=round(min(rps), 2),
            rps_max=round(max(rps), 2),
            p95_std=std([r.p95_ms for r in rs]),
            p99_std=std([r.p99_ms for r in rs]),
        )
        out.append(summary)
    for s in out:
        base = next((b for b in out if b.workload == s.workload and b.replicas == 1), None)
        if base and base.rps:
            s.scaling_efficiency = round(s.rps / (s.replicas * base.rps), 3)
    return out


def to_markdown(results: list[Result], meta: dict) -> str:
    lines = [
        f"# Benchmark run {meta['started']}",
        "",
        f"Host: {meta['host']}. Load generator: Locust {meta['locust']} in a container on "
        f"the stack network, {LOCUST_PROCESSES} worker processes, same machine as the stack. "
        "Closed-loop users, no think time. Accounts pre-provisioned outside the window.",
        "",
        f"Each row: mean of {meta.get('repeats', 1)} measured run(s) after a "
        f"{meta.get('warmup_s', 0)} s warmup; +/- is the standard deviation across runs.",
        "",
        "| Run | Replicas | Workload | Users | Req/s | Scaling eff. | p50 ms | p95 ms | p99 ms "
        "| Errors % | 429s | Lock timeouts | Other failures | PG conns max / mean "
        "| CPU % (mid-run) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        cpu = ", ".join(f"{name} {value:.0f}" for name, value in sorted(r.cpu_percent.items()))
        spread = f" +/- {r.rps_std:.0f}" if r.rps_std is not None else ""
        p95 = f" +/- {r.p95_std:.0f}" if r.p95_std is not None else ""
        eff = f"{r.scaling_efficiency:.0%}" if r.scaling_efficiency is not None else "-"
        lines.append(
            f"| {r.label} | {r.replicas} | {r.workload} | {r.users} | {r.rps:.0f}{spread} "
            f"| {eff} | {r.p50_ms:.0f} | {r.p95_ms:.0f}{p95} | {r.p99_ms:.0f} "
            f"| {r.error_pct:.2f} | {r.rejected_429} | {r.lock_timeouts} | {r.other_failures} "
            f"| {r.pg_connections_max} / {r.pg_connections_mean} | {cpu} |"
        )
    notes = [f"- {r.label}: {n}" for r in results for n in r.notes]
    return "\n".join(lines + (["", *notes] if notes else []) + [""])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--users", type=int, default=64)
    parser.add_argument("--replicas", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument(
        "--skip-quota-run", action="store_true", help="skip the small-quota rate-limit run"
    )
    parser.add_argument("--repeats", type=int, default=3, help="measured runs per configuration")
    parser.add_argument("--warmup", type=int, default=10, help="unmeasured seconds before each")
    parser.add_argument(
        "--workloads",
        nargs="+",
        default=list(WORKLOADS),
        choices=list(WORKLOADS),
        help="Locust user classes to run",
    )
    parser.add_argument(
        "--source", default="cli", choices=["cli", "controller"], help="who started the run"
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=RESULTS,
        help="where to write results (default benchmarks/results, shown in the Performance Lab)",
    )
    args = parser.parse_args()
    results_dir: Path = args.results_dir.resolve()
    if any(r not in (1, 2, 4) for r in args.replicas):
        parser.error("--replicas accepts only 1, 2 and 4")
    stack_control.assert_isolated(PROJECT)
    stack_control.ensure_local(PROJECT.base_url)
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = results_dir / stamp
    work.mkdir()
    meta = {
        "started": stamp,
        "host": subprocess.run(
            ["docker", "info", "--format", "{{.NCPU}} CPUs / {{.MemTotal}} bytes (Docker VM)"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip(),
        "locust": LOCUST_IMAGE.rsplit(":", 1)[1],
        "duration_s": args.duration,
        "users": args.users,
        "replicas": args.replicas,
        "workloads": args.workloads,
        "repeats": args.repeats,
        "warmup_s": args.warmup,
        "database": BENCH_DB,
        "compose_project": PROJECT.name,
        "source": args.source,
        "environment": (
            "isolated local Docker Compose project (gateway, replicas, PostgreSQL, Redis; no "
            "broker or event workers); load generator in a container on the same host"
        ),
    }
    results: list[Result] = []
    runs: list[Result] = []
    accounts_file = work / "accounts.json"
    try:
        configure(2, BENCH_ENV)
        print(f"== provisioning {args.users} funded account pairs")
        provision_accounts(args.users, accounts_file)
        for replicas in args.replicas:
            print(f"== {replicas} replica(s)")
            configure(replicas, BENCH_ENV)
            for workload in args.workloads:
                if args.warmup:
                    warmup(workload, args.users, args.warmup, accounts_file)
                for n in range(1, args.repeats + 1):
                    label = f"scale-{replicas}-{workload}"
                    result = Result(label, replicas, workload, args.users, args.duration, run=n)
                    run_locust(result, work / f"{label}-run{n}", accounts_file)
                    print(
                        f"   {workload} run {n}/{args.repeats}: {result.rps:.0f} req/s "
                        f"p95 {result.p95_ms:.0f} ms errors {result.error_pct}% "
                        f"429s {result.rejected_429} lock timeouts {result.lock_timeouts}"
                    )
                    runs.append(result)
        results.extend(summarize(runs))

        if not args.skip_quota_run:
            print("== small write quota (50 burst, 20/s refill), 2 replicas")
            configure(2, SMALL_QUOTA)
            stack_control.reset_rate_limits(PROJECT.redis_url)
            result = Result("small-quota", 2, "MixedTransferUser", args.users, args.duration)
            result.notes.append(
                "write policy: burst 50, refill 20 tokens/s (shared by all replicas)"
            )
            run_locust(result, work / result.label, accounts_file)
            expected = 50 + 20 * args.duration
            result.notes.append(
                f"admitted transfers: {result.transfers_admitted} "
                f"(quota allows at most ~{expected} = 50 burst + 20/s x {args.duration} s)"
            )
            print(f"   admitted transfers {result.transfers_admitted}, 429s {result.rejected_429}")
            results.append(result)
    finally:
        # Only the throw-away benchmark project is removed; the dev stack was never touched.
        print(f"== removing the isolated benchmark project {PROJECT.name}")
        stack_control.teardown(PROJECT)

    payload = {
        "meta": meta,
        "results": [asdict(r) for r in results],
        "runs": [asdict(r) for r in runs],
    }
    (results_dir / f"{stamp}.json").write_text(json.dumps(payload, indent=2))
    (results_dir / f"{stamp}.md").write_text(to_markdown(results, meta))
    print(f"\nWrote {results_dir / (stamp + '.md')}")
    print(to_markdown(results, meta))


if __name__ == "__main__":
    main()
