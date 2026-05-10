"""Redis client dependency for FastAPI."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Request
from redis.asyncio import Redis


async def get_redis(request: Request) -> AsyncGenerator[Redis, None]:
    """FastAPI dependency — yields an async Redis client backed by the app-level pool."""
    pool = request.app.state.redis_pool
    client: Redis = Redis(connection_pool=pool)
    try:
        yield client
    finally:
        await client.aclose()
