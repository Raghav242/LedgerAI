from fastapi import APIRouter

from app.api.routes import accounts, transfers


def build_api_router(*, include_dev_endpoints: bool) -> APIRouter:
    router = APIRouter()
    router.include_router(accounts.router)
    router.include_router(transfers.router)
    if include_dev_endpoints:
        router.include_router(accounts.dev_router)
    return router
