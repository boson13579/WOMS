"""Redis-backed distributed scheduling lock.

Used by the scheduling engine (Celery task) via `scheduling_lock_context`.
The CRUD router calls `is_scheduling_locked` to gate write operations.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

SCHEDULING_LOCK_KEY = "scheduling:lock"
SCHEDULING_LOCK_TTL_SECONDS = 300  # 5 minutes — prevents deadlock on engine crash

# Atomic compare-and-delete: only removes the key when the stored value matches the token.
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


async def acquire_scheduling_lock(redis: Redis) -> str | None:
    """Attempt to acquire the scheduling lock.

    Returns a unique token (str) if acquired, None if already held.
    Uses SET NX EX for an atomic check-and-set.
    """
    token = str(uuid.uuid4())
    result = await redis.set(SCHEDULING_LOCK_KEY, token, nx=True, ex=SCHEDULING_LOCK_TTL_SECONDS)
    return token if result is True else None


async def release_scheduling_lock(redis: Redis, token: str) -> bool:
    """Release the scheduling lock only if the token matches the stored value.

    Returns True if the lock was released, False if the token did not match.
    """
    result = await redis.eval(_RELEASE_SCRIPT, 1, SCHEDULING_LOCK_KEY, token)  # type: ignore[misc]
    return bool(result)


async def is_scheduling_locked(redis: Redis) -> bool:
    """Return True if the scheduling lock is currently held."""
    return bool(await redis.exists(SCHEDULING_LOCK_KEY))


@asynccontextmanager
async def scheduling_lock_context(redis: Redis) -> AsyncGenerator[None, None]:
    """Context manager for the scheduling engine (Celery task).

    Acquires the lock on enter and releases it on exit (even on error).
    Raises RuntimeError if the lock cannot be acquired.
    """
    token = await acquire_scheduling_lock(redis)
    if token is None:
        msg = "Scheduling lock is already held by another process."
        raise RuntimeError(msg)
    try:
        yield
    finally:
        await release_scheduling_lock(redis, token)
