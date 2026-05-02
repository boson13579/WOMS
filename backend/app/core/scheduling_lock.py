"""Redis-backed distributed scheduling lock.

Used by the scheduling engine (Celery task) via `scheduling_lock_context`.
The CRUD router calls `is_scheduling_locked` to gate write operations.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

SCHEDULING_LOCK_KEY = "scheduling:lock"
SCHEDULING_LOCK_TTL_SECONDS = 300  # 5 minutes — prevents deadlock on engine crash


async def acquire_scheduling_lock(redis: Redis) -> bool:
    """Attempt to acquire the scheduling lock.

    Returns True if the lock was acquired, False if already held.
    Uses SET NX EX for an atomic check-and-set.
    """
    result = await redis.set(SCHEDULING_LOCK_KEY, "1", nx=True, ex=SCHEDULING_LOCK_TTL_SECONDS)
    return result is True


async def release_scheduling_lock(redis: Redis) -> None:
    """Release the scheduling lock by deleting the key."""
    await redis.delete(SCHEDULING_LOCK_KEY)


async def is_scheduling_locked(redis: Redis) -> bool:
    """Return True if the scheduling lock is currently held."""
    return bool(await redis.exists(SCHEDULING_LOCK_KEY))


@asynccontextmanager
async def scheduling_lock_context(redis: Redis) -> AsyncGenerator[None, None]:
    """Context manager for the scheduling engine (Celery task).

    Acquires the lock on enter and releases it on exit (even on error).
    Raises RuntimeError if the lock cannot be acquired.
    """
    acquired = await acquire_scheduling_lock(redis)
    if not acquired:
        msg = "Scheduling lock is already held by another process."
        raise RuntimeError(msg)
    try:
        yield
    finally:
        await release_scheduling_lock(redis)
