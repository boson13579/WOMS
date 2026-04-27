"""API v1 routers."""

from fastapi import APIRouter

from app.api.v1 import health

# Aggregate router — `app.main` includes only this single router with prefix
# `/api/v1`, keeping `main.py` free of per-feature wiring.
api_router = APIRouter()
api_router.include_router(health.router, tags=["meta"])
