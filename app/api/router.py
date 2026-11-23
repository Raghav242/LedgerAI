from fastapi import APIRouter

from app.api.routes import (
    accounts,
    ai,
    lab,
    ledger,
    observability,
    performance,
    reconciliation,
    stats,
    transfers,
)


def build_api_router(*, include_dev_endpoints: bool) -> APIRouter:
    router = APIRouter()
    router.include_router(accounts.router)
    router.include_router(transfers.router)
    router.include_router(reconciliation.router)
    router.include_router(ledger.router)
    router.include_router(stats.router)
    if include_dev_endpoints:
        router.include_router(accounts.dev_router)
        # The Experiment Lab mints simulated money for its own accounts and generates
        # load: development-only, like the funding endpoint.
        router.include_router(lab.router)
        # Read-only live topology, event pipeline and traces (reveals internal topology).
        router.include_router(observability.router)
        # Read-only AI anomaly results and model card (the worker scores asynchronously).
        router.include_router(ai.router)
        router.include_router(performance.router)
    return router
