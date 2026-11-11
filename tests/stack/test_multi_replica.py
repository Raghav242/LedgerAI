import subprocess
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import text

from tests.stack.conftest import WRITE_CAPACITY, Stack

RETRYABLE_STATUS = {502, 503, 504}


def _client(stack: Stack) -> httpx.Client:
    return httpx.Client(base_url=stack.base_url, timeout=30)


def _account(http: httpx.Client, funded: int = 0) -> str:
    account_id = http.post("/api/v1/accounts", json={"name": f"stack-{uuid.uuid4().hex[:6]}"})
    account_id.raise_for_status()
    identifier = account_id.json()["id"]
    if funded:
        http.post(
            f"/api/v1/accounts/{identifier}/fund", json={"amount_minor": funded}
        ).raise_for_status()
    return identifier


def _transfer(
    http: httpx.Client, source: str, destination: str, amount: int, key: str
) -> httpx.Response:
    return http.post(
        "/api/v1/transfers",
        json={
            "source_account_id": source,
            "destination_account_id": destination,
            "amount_minor": amount,
            "currency": "USD",
        },
        headers={"Idempotency-Key": key},
    )


def _idempotency_keys_present(stack: Stack, keys: list[str]) -> set[str]:
    with stack.db.connect() as conn:
        rows = conn.execute(
            text("SELECT idempotency_key FROM idempotency_records WHERE idempotency_key = ANY(:k)"),
            {"k": keys},
        )
        return {row[0] for row in rows}


def _reconciled(http: httpx.Client) -> dict:
    report = http.get("/api/v1/reconciliation").json()
    assert report["ok"], report
    return report


def test_both_replicas_serve_requests(stack: Stack) -> None:
    with _client(stack) as http:
        served = Counter(http.get("/health/live").headers["X-Served-By"] for _ in range(20))
    assert set(served) == {"api-1", "api-2"}
    assert served["api-1"] == served["api-2"] == 10  # round robin, no other traffic


def test_rate_limit_is_global_across_replicas(stack: Stack, fresh_quota: None) -> None:
    with _client(stack) as http:
        source, destination = _account(http, funded=100_000), _account(http)
    from stack_control import reset_rate_limits

    reset_rate_limits()  # setup spent write tokens; start the measurement with a full bucket
    keys = [str(uuid.uuid4()) for _ in range(100)]

    def send(key: str) -> httpx.Response:
        with _client(stack) as http:
            return _transfer(http, source, destination, 1, key)

    with ThreadPoolExecutor(max_workers=50) as pool:
        responses = list(pool.map(send, keys))

    statuses = Counter(r.status_code for r in responses)
    allowed = [r for r in responses if r.status_code == 201]
    assert statuses == {201: WRITE_CAPACITY, 429: 100 - WRITE_CAPACITY}
    # Admitted requests were served by BOTH replicas, yet the total equals ONE quota:
    # per-replica counters would have admitted up to 2 * WRITE_CAPACITY.
    assert {r.headers["X-Served-By"] for r in allowed} == {"api-1", "api-2"}

    rejected_keys = [k for k, r in zip(keys, responses, strict=True) if r.status_code == 429]
    allowed_keys = [k for k, r in zip(keys, responses, strict=True) if r.status_code == 201]
    # 429s never reached the transaction service: their keys were never claimed.
    assert _idempotency_keys_present(stack, rejected_keys) == set()
    assert _idempotency_keys_present(stack, allowed_keys) == set(allowed_keys)
    with _client(stack) as http:
        history = http.get(
            "/api/v1/transfers",
            params={"account_id": destination, "kind": "transfer", "limit": 100},
        ).json()["items"]
        assert len(history) == WRITE_CAPACITY
        assert http.get(f"/api/v1/accounts/{destination}").json()["balance_minor"] == WRITE_CAPACITY
        _reconciled(http)


def test_idempotency_holds_when_retries_land_on_different_replicas(
    stack: Stack, fresh_quota: None
) -> None:
    key = str(uuid.uuid4())
    with _client(stack) as http:
        source, destination = _account(http, funded=10_000), _account(http)
        responses = [_transfer(http, source, destination, 250, key) for _ in range(6)]
        balance = http.get(f"/api/v1/accounts/{source}").json()["balance_minor"]

    replicas = {r.headers["X-Served-By"] for r in responses}
    assert replicas == {"api-1", "api-2"}
    assert {r.status_code for r in responses} == {201}
    assert len({r.content for r in responses}) == 1  # byte-identical on every replica
    assert sum("Idempotent-Replayed" not in r.headers for r in responses) == 1
    assert balance == 10_000 - 250


def test_killing_a_replica_mid_traffic_does_not_corrupt_state(
    stack: Stack, fresh_quota: None
) -> None:
    """SIGKILL api-2 while 40 transfers are in flight. Clients retry 502/503/504 and
    connection errors with the SAME key; NGINX never re-sends a POST that reached a
    replica. Every key must end with exactly one committed transfer."""
    with _client(stack) as http:
        source, destination = _account(http, funded=1_000_000), _account(http)
    from stack_control import reset_rate_limits

    reset_rate_limits()
    keys = [str(uuid.uuid4()) for _ in range(40)]
    done = Counter()
    lock = threading.Lock()
    half_done = threading.Event()
    attempts: Counter[str] = Counter()

    def send(key: str) -> int:
        with _client(stack) as http:
            for _ in range(20):
                attempts[key] += 1
                try:
                    status = _transfer(http, source, destination, 100, key).status_code
                except httpx.TransportError:
                    status = 0
                if status not in RETRYABLE_STATUS and status != 0:
                    break
                time.sleep(0.25)  # back off before retrying the same key
            with lock:
                done["n"] += 1
                if done["n"] >= 10:
                    half_done.set()
            return status

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(send, k) for k in keys]
        assert half_done.wait(60)
        subprocess.run(["docker", "kill", "ledgerflow-api-2-1"], check=True, capture_output=True)
        statuses = [f.result(timeout=120) for f in futures]

    assert statuses == [201] * 40
    assert _idempotency_keys_present(stack, keys) == set(keys)
    with stack.db.connect() as conn:
        transfers = conn.execute(
            text(
                "SELECT count(*) FROM transfers "
                "WHERE destination_account_id = :d AND kind = 'transfer'"
            ),
            {"d": destination},
        ).scalar_one()
    assert transfers == 40  # no duplicates, no losses
    with _client(stack) as http:
        assert http.get(f"/api/v1/accounts/{destination}").json()["balance_minor"] == 4_000
        _reconciled(http)

    # `docker kill` counts as a manual stop, so the restart policy does not apply (a real
    # process crash would be restarted automatically). Recover explicitly.
    from stack_control import compose, wait_until_served_only_by

    compose("start", "api-2")
    wait_until_served_only_by(stack.base_url, {"api-1", "api-2"}, timeout=60)


@pytest.mark.parametrize("path", ["/health/live", "/api/v1/accounts?limit=1"])
def test_requests_keep_working_with_one_replica_stopped(stack: Stack, path: str) -> None:
    from stack_control import compose, wait_until_served_only_by

    compose("stop", "api-2")
    try:
        with _client(stack) as http:
            statuses = [http.get(path).status_code for _ in range(10)]
            replicas = {http.get(path).headers["X-Served-By"] for _ in range(4)}
        assert statuses == [200] * 10  # connection errors to api-2 are retried by NGINX
        assert replicas == {"api-1"}
    finally:
        compose("start", "api-2")
        wait_until_served_only_by(stack.base_url, {"api-1", "api-2"}, timeout=60)
