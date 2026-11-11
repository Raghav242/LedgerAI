"""End-to-end tests through NGINX against the real multi-replica stack.

Opt-in (`pytest -m stack`): the session fixture reconfigures the local Compose stack to
use a SEPARATE database (ledgerflow_stacktest) and a deterministic write quota, and
restores the default configuration afterwards. Development data is never touched.
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

STACK_DB = "ledgerflow_stacktest"
WRITE_CAPACITY = 60
# Large read quota, write quota that does not refill during a test (1 token per hour):
# allowed/rejected counts are then exact, independent of timing.
TEST_OVERRIDES = {
    "API_POSTGRES_DB": STACK_DB,
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
    db: Engine


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    base_url = stack_control.DEFAULT_BASE_URL
    stack_control.ensure_local(base_url)
    stack_control.ensure_database(STACK_DB)
    stack_control.deploy(TEST_OVERRIDES)
    stack_control.wait_ready(base_url)
    settings = Settings().model_copy(update={"postgres_db": STACK_DB})  # type: ignore[call-arg]
    engine = create_engine(settings.database_url)
    try:
        yield Stack(base_url=base_url, db=engine)
    finally:
        engine.dispose()
        stack_control.restore_default()


@pytest.fixture
def fresh_quota(stack: Stack) -> None:
    """Every test starts with a full write bucket."""
    stack_control.reset_rate_limits()
