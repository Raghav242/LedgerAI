"""End-to-end tests through NGINX against the real multi-replica stack.

Opt-in (`pytest -m stack`). Phase 6B: the session fixture brings up the ISOLATED Compose
project `ledgerflow-stacktest` (scripts/stack_control.py STACK): the complete stack with
its own containers, network (172.30.0.0/24), volumes, loopback ports (gateway
127.0.0.1:8280) and database, then removes it again, volumes included. The development
stack is never redeployed, restarted or read, so it can keep running meanwhile.
"""

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import stack_control  # noqa: E402

PROJECT = stack_control.STACK
STACK_DB = stack_control.STACK_DB
WRITE_CAPACITY = 60
# Large read quota, write quota that does not refill during a test (1 token per hour):
# allowed/rejected counts are then exact, independent of timing.
TEST_OVERRIDES = {
    "ENABLE_DEV_ENDPOINTS": "true",
    "EXPOSE_REPLICA_HEADER": "true",
    "RATE_LIMIT_WRITE_CAPACITY": str(WRITE_CAPACITY),
    "RATE_LIMIT_WRITE_REFILL_TOKENS": "1",
    "RATE_LIMIT_WRITE_REFILL_INTERVAL_MS": "3600000",
    "RATE_LIMIT_READ_CAPACITY": "100000",
    "RATE_LIMIT_READ_REFILL_TOKENS": "100000",
    "RATE_LIMIT_READ_REFILL_INTERVAL_MS": "1000",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/stack" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.stack)


@dataclass(frozen=True)
class Stack:
    base_url: str
    db: Engine  # the stack-test database, as the schema-owning migration role
    project: stack_control.Project

    def compose(self, *args: str) -> None:
        stack_control.compose(*args, project=self.project)

    def container(self, service: str) -> str:
        return self.project.service_container(service)


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    stack_control.assert_isolated(PROJECT)
    stack_control.assert_isolated(PROJECT, stack_control.BENCH)
    stack_control.ensure_local(PROJECT.base_url)
    stack_control.teardown(PROJECT)  # leftovers of an interrupted earlier session
    try:
        stack_control.deploy(TEST_OVERRIDES, project=PROJECT)
        stack_control.wait_ready(PROJECT.base_url)
        settings = Settings().model_copy(  # type: ignore[call-arg]
            update={
                "postgres_db": STACK_DB,
                "postgres_port": int(PROJECT.env["POSTGRES_PORT"]),
            }
        )
        engine = create_engine(settings.database_url)
        try:
            yield Stack(base_url=PROJECT.base_url, db=engine, project=PROJECT)
        finally:
            engine.dispose()
    finally:
        stack_control.teardown(PROJECT)


@pytest.fixture
def fresh_quota(stack: Stack) -> None:
    """Every test starts with a full write bucket."""
    stack_control.reset_rate_limits(stack.project.redis_url)
