"""WebSocket dispatch — publishes events to Redis pub/sub.

The actual fan-out to connected sockets lives in :mod:`app.api.v1.websocket`,
which subscribes to the same channel inside the FastAPI process and pushes
to its local connections. Splitting publisher (here, sync) from subscriber
(there, async) lets sync Celery workers and async FastAPI handlers share a
WebSocket transport without sharing an event loop.

Public surface — these two are the contract the scheduler relies on:

- ``broadcast(message)`` — fan out to every connected client.
- ``notify_user(user_id, message)`` — push to one user's open sessions.

Both functions are synchronous and safe to call from a Celery task.
"""

from __future__ import annotations

import json
import uuid
from functools import lru_cache
from typing import Any

import structlog
from redis import Redis

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

__all__ = ["EVENT_CHANNEL", "broadcast", "notify_user"]

# Single Redis pub/sub channel for every WebSocket-bound event. The envelope
# carries the discriminator (`kind`) so the subscriber can route per event
# type without us paying for one channel per type.
EVENT_CHANNEL = "schedule:ws:events"


@lru_cache(maxsize=1)
def _redis() -> Redis:
    """Lazy module-level Redis client for the publisher path."""
    return Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True)


def notify_user(*, user_id: uuid.UUID, message: dict[str, Any]) -> None:
    """Publish *message* targeted at every active session of *user_id*."""
    envelope = {
        "kind": "notify_user",
        "user_id": str(user_id),
        "payload": message,
    }
    try:
        _redis().publish(EVENT_CHANNEL, json.dumps(envelope))
    except Exception as exc:
        # Pub/sub is best-effort — losing a notification must not break the
        # caller's transaction. Just log and move on.
        logger.warning(
            "websocket.notify_user.publish_failed",
            user_id=str(user_id),
            error=str(exc),
        )
        return
    logger.info(
        "websocket.notify_user.published",
        user_id=str(user_id),
        message=message,
    )


def broadcast(message: dict[str, Any]) -> None:
    """Publish *message* to every connected client."""
    envelope = {"kind": "broadcast", "payload": message}
    try:
        _redis().publish(EVENT_CHANNEL, json.dumps(envelope))
    except Exception as exc:
        logger.warning("websocket.broadcast.publish_failed", error=str(exc))
        return
    logger.info("websocket.broadcast.published", message=message)
