"""API v1 routers."""

from fastapi import APIRouter

from app.api.v1 import auth, health, orders, schedule, users, websocket

# Aggregate router — `app.main` includes only this single router with prefix
# `/api/v1`, keeping `main.py` free of per-feature wiring.
api_router = APIRouter()
api_router.include_router(health.router, tags=["meta"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(orders.router, prefix="/orders", tags=["orders"])

api_router.include_router(users.router, prefix="/users", tags=["users"])

api_router.include_router(schedule.router, prefix="/schedule", tags=["schedule"])
# WebSocket router has no prefix — endpoint is /api/v1/ws (single channel).
api_router.include_router(websocket.router, tags=["websocket"])
