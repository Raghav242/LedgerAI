"""The allow-list of runnable experiments. Nothing outside this mapping can ever run."""

from collections.abc import Awaitable, Callable
from typing import Any

from app.lab.context import ExperimentContext, ExperimentOutcome
from app.lab.experiments import anomaly, concurrency, idempotency, load_balancing, rate_limiting

ExperimentFn = Callable[[ExperimentContext, Any], Awaitable[ExperimentOutcome]]

EXPERIMENTS: dict[str, ExperimentFn] = {
    "concurrency": concurrency.run,
    "idempotency": idempotency.run,
    "rate_limiting": rate_limiting.run,
    "load_balancing": load_balancing.run,
    "anomaly_scenario": anomaly.run,
}

# Hard ceiling per run, on top of the bounded parameters and per-request timeouts.
RUN_TIMEOUT_SECONDS = 120
