"""Locust workloads for LedgerFlow AI (run through NGINX).

    locust -f benchmarks/locustfile.py ReadHeavyUser --host http://127.0.0.1:8080
    locust -f benchmarks/locustfile.py MixedTransferUser --host http://127.0.0.1:8080

Users are closed-loop (no think time): each sends its next request as soon as the
previous one returns, so throughput measures the system, not a configured arrival rate.
429s are recorded as failures named "429 rate limited" so they can be counted apart
from real errors.
"""

import itertools
import json
import os
import random
import time
import uuid

from locust import HttpUser, constant, task

FUNDING_MINOR = 10_000_000

# Pre-provisioned, pre-funded account pairs (written by run_benchmarks.py). Using them keeps
# account creation and funding (which all lock the single system funding account) out of
# the measured window. Without the file, users create their own accounts in on_start.
_ACCOUNTS_FILE = os.environ.get("LEDGERFLOW_BENCH_ACCOUNTS")
_PAIRS: list[list[str]] = json.load(open(_ACCOUNTS_FILE)) if _ACCOUNTS_FILE else []  # noqa: SIM115
_USERS_PER_PROCESS = int(os.environ.get("LEDGERFLOW_USERS_PER_PROCESS", "1000"))
_local_index = itertools.count()


def _next_pair(user: HttpUser) -> list[str]:
    """A pair no other user is using: worker processes take disjoint index ranges."""
    worker = getattr(user.environment.runner, "worker_index", 0)
    return _PAIRS[(worker * _USERS_PER_PROCESS + next(_local_index)) % len(_PAIRS)]


def _check(response, expected: tuple[int, ...] = (200,)) -> None:  # type: ignore[no-untyped-def]
    if response.status_code == 429:
        response.failure("429 rate limited")
    elif response.status_code not in expected:
        response.failure(f"HTTP {response.status_code}")
    else:
        response.success()


class _LedgerUser(HttpUser):
    abstract = True
    wait_time = constant(0)

    def _setup_post(self, path: str, body: dict) -> dict:
        """Setup writes retry on 429 (they were not executed) so tiny quotas still work."""
        for _ in range(200):
            response = self.client.post(path, json=body, name="setup")
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            time.sleep(float(response.headers.get("Retry-After", "1")))
        raise RuntimeError("setup could not get past the rate limiter")

    def _new_account(self, funded: int = 0) -> str:
        account = self._setup_post("/api/v1/accounts", {"name": f"bench-{uuid.uuid4().hex[:8]}"})
        if funded:
            self._setup_post(f"/api/v1/accounts/{account['id']}/fund", {"amount_minor": funded})
        return account["id"]


class ReadHeavyUser(_LedgerUser):
    """Workload A: account lookups, balances, listings and liveness checks."""

    def on_start(self) -> None:
        self.account_id = _next_pair(self)[0] if _PAIRS else self._new_account(funded=1_000)

    @task(3)
    def get_account(self) -> None:
        with self.client.get(
            f"/api/v1/accounts/{self.account_id}", name="GET /accounts/{id}", catch_response=True
        ) as response:
            _check(response)

    @task(3)
    def get_balance(self) -> None:
        with self.client.get(
            f"/api/v1/accounts/{self.account_id}/balance",
            name="GET /accounts/{id}/balance",
            catch_response=True,
        ) as response:
            _check(response)

    @task(1)
    def list_accounts(self) -> None:
        with self.client.get(
            "/api/v1/accounts?limit=20", name="GET /accounts", catch_response=True
        ) as response:
            _check(response)

    @task(1)
    def liveness(self) -> None:
        with self.client.get("/health/live", name="GET /health/live", catch_response=True) as r:
            _check(r)


class MixedTransferUser(_LedgerUser):
    """Workload B: transfers between this user's OWN two accounts (no shared hot account
    across users, so the comparison measures scaling, not lock queueing) plus reads."""

    def on_start(self) -> None:
        if _PAIRS:
            self.accounts = _next_pair(self)
        else:
            self.accounts = [self._new_account(funded=FUNDING_MINOR) for _ in range(2)]

    @task(3)
    def transfer(self) -> None:
        source, destination = random.sample(self.accounts, 2)
        with self.client.post(
            "/api/v1/transfers",
            json={
                "source_account_id": source,
                "destination_account_id": destination,
                "amount_minor": 1,
                "currency": "USD",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
            name="POST /transfers",
            catch_response=True,
        ) as response:
            _check(response, expected=(201,))

    @task(1)
    def balance(self) -> None:
        with self.client.get(
            f"/api/v1/accounts/{self.accounts[0]}/balance",
            name="GET /accounts/{id}/balance",
            catch_response=True,
        ) as response:
            _check(response)
