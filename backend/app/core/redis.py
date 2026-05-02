"""Redis client dependency for FastAPI."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.core.config import get_settings


async def get_redis() -> AsyncGenerator[Redis, None]:
    """FastAPI dependency — yields an async Redis client per request."""
    settings = get_settings()
    client: Redis = aioredis.from_url(  # type: ignore[no-untyped-call]
        str(settings.REDIS_URL),
        decode_responses=True,
    )
    try:
        yield client
    finally:
        await client.aclose()
