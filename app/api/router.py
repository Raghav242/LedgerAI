from fastapi import APIRouter

from app.api.routes import accounts, lab, ledger, reconciliation, stats, transfers


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
    return router
