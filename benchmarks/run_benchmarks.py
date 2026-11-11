"""Benchmark 1 / 2 / 4 API replicas with Locust, plus a small-quota rate-limit run.

LOCAL ONLY. Redeploys the Compose stack against a separate database (ledgerflow_bench)
with quotas high enough that rate limiting does not shape the scaling comparison, then
restores the default stack. Results: benchmarks/results/<timestamp>.{json,md}

Measurement hygiene:
- Accounts are created and funded BEFORE any measured run (funding locks the single
  system account; doing it inside the window would measure that hot row instead).
- Locust runs in a Linux container on the stack's internal network with several worker
  processes, so the load generator is not limited to one Python process and requests
  bypass Docker Desktop's host port forwarding.

    python benchmarks/run_benchmarks.py [--duration 30] [--users 64] [--replicas 1 2 4]
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
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import stack_control  # noqa: E402

BENCH_DB = "ledgerflow_bench"
LOCUSTFILE = ROOT / "benchmarks" / "locustfile.py"
RESULTS = ROOT / "benchmarks" / "results"
LOCUST_IMAGE = "locustio/locust:2.47.0"
LOCUST_PROCESSES = 4
STACK_NETWORK = "ledgerflow_backend"
INTERNAL_URL = "http://nginx:8080"
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
    "API_POSTGRES_DB": BENCH_DB,
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
    transfer_rps: float | None = None  # all POST /transfers attempts per second
    transfers_admitted: int | None = None  # POST /transfers that were not 429
    notes: list[str] = field(default_factory=list)


def configure(replicas: int, env: dict[str, str]) -> None:
    stack_control.remove_extra_replicas()
    profiles = ("scale4",) if replicas == 4 else ()
    stack_control.deploy(env, profiles=profiles)
    if replicas == 1:
        stack_control.compose("stop", "api-2")
    expected = {f"api-{i}" for i in range(1, replicas + 1)}
    stack_control.wait_until_served_only_by(stack_control.DEFAULT_BASE_URL, expected, probes=16)


class ConnectionSampler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.samples: list[int] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(subprocess.CalledProcessError):
                self.samples.append(stack_control.postgres_client_connections(BENCH_DB))
            self._stop.wait(1)

    def stop(self) -> None:
        self._stop.set()
        self.join()


def sample_cpu(result: Result, delay_s: float) -> threading.Thread:
    """One `docker stats` snapshot half-way through the run (100% = one CPU core)."""

    def take() -> None:
        time.sleep(delay_s)
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        for line in out.splitlines():
            name, _, cpu = line.partition("\t")
            if name.startswith("ledgerflow-") and cpu.endswith("%"):
                short = name.removeprefix("ledgerflow-").removesuffix("-1")
                result.cpu_percent[short] = float(cpu.rstrip("%"))

    thread = threading.Thread(target=take, daemon=True)
    thread.start()
    return thread


def provision_accounts(pairs: int, path: Path) -> None:
    """Create and fund `pairs` account pairs through the API (unlimited quota)."""
    import httpx

    accounts: list[list[str]] = []
    with httpx.Client(base_url=stack_control.DEFAULT_BASE_URL, timeout=30) as client:
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


def run_locust(result: Result, csv_prefix: Path, accounts_file: Path) -> None:
    sampler = ConnectionSampler()
    sampler.start()
    cpu = sample_cpu(result, result.duration_s / 2)
    mount = f"{(ROOT / 'benchmarks').as_posix()}:/mnt/bench"
    in_container = f"/mnt/bench/{csv_prefix.relative_to(ROOT / 'benchmarks').as_posix()}"
    accounts_in_container = (
        f"/mnt/bench/{accounts_file.relative_to(ROOT / 'benchmarks').as_posix()}"
    )
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            STACK_NETWORK,
            "-v",
            mount,
            "-e",
            f"LEDGERFLOW_BENCH_ACCOUNTS={accounts_in_container}",
            "-e",
            f"LEDGERFLOW_USERS_PER_PROCESS={-(-result.users // LOCUST_PROCESSES)}",
            LOCUST_IMAGE,
            "-f",
            "/mnt/bench/locustfile.py",
            result.workload,
            "--headless",
            "-u",
            str(result.users),
            "-r",
            str(result.users),
            "-t",
            f"{result.duration_s}s",
            "--host",
            INTERNAL_URL,
            "--processes",
            str(LOCUST_PROCESSES),
            "--csv",
            in_container,
            "--reset-stats",
            "--only-summary",
            "--loglevel",
            "WARNING",
            "--exit-code-on-error",
            "0",
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
    transfers = next((r for r in rows if r["Name"] == "POST /transfers"), None)
    transfer_429s = 0

    with open(f"{csv_prefix}_failures.csv", newline="") as f:
        for row in csv.DictReader(f):
            if "429" in row["Error"]:
                result.rejected_429 += int(row["Occurrences"])
                if row["Name"] == "POST /transfers":
                    transfer_429s += int(row["Occurrences"])
            else:
                result.other_failures += int(row["Occurrences"])
    if transfers is not None:
        result.transfer_rps = float(transfers["Requests/s"])
        result.transfers_admitted = int(transfers["Request Count"]) - transfer_429s
    if sampler.samples:
        result.pg_connections_max = max(sampler.samples)
        result.pg_connections_mean = round(statistics.mean(sampler.samples), 1)


def to_markdown(results: list[Result], meta: dict) -> str:
    lines = [
        f"# Benchmark run {meta['started']}",
        "",
        f"Host: {meta['host']}. Load generator: Locust {meta['locust']} in a container on "
        f"the stack network, {LOCUST_PROCESSES} worker processes, same machine as the stack. "
        "Closed-loop users, no think time. Accounts pre-provisioned outside the window.",
        "",
        "| Run | Replicas | Workload | Users | Req/s | p50 ms | p95 ms | p99 ms | Errors % "
        "| 429s | Other failures | PG conns max / mean | CPU % (mid-run) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        cpu = ", ".join(f"{name} {value:.0f}" for name, value in sorted(r.cpu_percent.items()))
        lines.append(
            f"| {r.label} | {r.replicas} | {r.workload} | {r.users} | {r.rps:.0f} "
            f"| {r.p50_ms:.0f} | {r.p95_ms:.0f} | {r.p99_ms:.0f} | {r.error_pct:.2f} "
            f"| {r.rejected_429} | {r.other_failures} "
            f"| {r.pg_connections_max} / {r.pg_connections_mean} | {cpu} |"
        )
    notes = [f"- {r.label}: {n}" for r in results for n in r.notes]
    return "\n".join(lines + (["", *notes] if notes else []) + [""])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--users", type=int, default=64)
    parser.add_argument("--replicas", type=int, nargs="+", default=[1, 2, 4])
    args = parser.parse_args()
    stack_control.ensure_local(stack_control.DEFAULT_BASE_URL)
    stack_control.ensure_database(BENCH_DB)
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = RESULTS / stamp
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
    }
    results: list[Result] = []
    accounts_file = work / "accounts.json"
    try:
        configure(2, BENCH_ENV)
        print(f"== provisioning {args.users} funded account pairs")
        provision_accounts(args.users, accounts_file)
        for replicas in args.replicas:
            print(f"== {replicas} replica(s)")
            configure(replicas, BENCH_ENV)
            for workload in ("ReadHeavyUser", "MixedTransferUser"):
                result = Result(
                    f"scale-{replicas}-{workload}", replicas, workload, args.users, args.duration
                )
                run_locust(result, work / result.label, accounts_file)
                print(
                    f"   {workload}: {result.rps:.0f} req/s p95 {result.p95_ms:.0f} ms "
                    f"errors {result.error_pct}% 429s {result.rejected_429}"
                )
                results.append(result)

        print("== small write quota (50 burst, 20/s refill), 2 replicas")
        configure(2, SMALL_QUOTA)
        stack_control.reset_rate_limits()
        result = Result("small-quota", 2, "MixedTransferUser", args.users, args.duration)
        result.notes.append("write policy: burst 50, refill 20 tokens/s (shared by all replicas)")
        run_locust(result, work / result.label, accounts_file)
        expected = 50 + 20 * args.duration
        result.notes.append(
            f"admitted transfers: {result.transfers_admitted} "
            f"(quota allows at most ~{expected} = 50 burst + 20/s x {args.duration} s)"
        )
        print(f"   admitted transfers {result.transfers_admitted}, 429s {result.rejected_429}")
        results.append(result)
    finally:
        print("== restoring default stack")
        stack_control.restore_default()

    payload = {"meta": meta, "results": [asdict(r) for r in results]}
    (RESULTS / f"{stamp}.json").write_text(json.dumps(payload, indent=2))
    (RESULTS / f"{stamp}.md").write_text(to_markdown(results, meta))
    print(f"\nWrote {RESULTS / (stamp + '.md')}")
    print(to_markdown(results, meta))


if __name__ == "__main__":
    main()
