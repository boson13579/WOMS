"""Redis client dependency for FastAPI."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import cast

import structlog
from fastapi import Request
from redis.asyncio import ConnectionPool, Redis

logger = structlog.get_logger(__name__)


async def get_redis(request: Request) -> AsyncGenerator[Redis, None]:
    """FastAPI dependency — yields an async Redis client backed by the app-level pool."""
    pool = cast(ConnectionPool, request.app.state.redis_pool)
    client: Redis = Redis(connection_pool=pool)
    try:
        yield client
    finally:
        try:
            await client.aclose()
        except Exception:
            logger.warning("redis.aclose_failed", exc_info=True)
