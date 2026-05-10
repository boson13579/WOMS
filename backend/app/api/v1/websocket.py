"""WebSocket endpoint, connection registry, and Redis-bridge consumer.

Architecture
------------
Workers (sync, separate process) call :func:`app.services.websocket.broadcast`
or :func:`app.services.websocket.notify_user`, which ``PUBLISH`` an envelope
onto a Redis pub/sub channel. Inside the FastAPI process this module runs a
background asyncio task (:func:`event_consumer_loop`) that ``SUBSCRIBE``s to
the same channel, decodes envelopes, and fans them out via
:class:`ConnectionManager` to the WebSockets currently held by *this*
process.

The split keeps the worker-side API synchronous (a Celery task can just call
``broadcast(...)``) while letting the async FastAPI runtime own the live
connections. Multiple FastAPI workers each subscribe independently — Redis
fans the same message to every subscriber, and each manager only delivers
to its locally-connected sockets, so horizontal scaling is automatic.

Authentication
--------------
Browsers can't set ``Authorization`` headers on a ``WebSocket`` constructor,
so we accept the JWT as a ``?token=`` query parameter. Validation reuses
:func:`app.core.security.decode_access_token`. Bad tokens cause
``websocket.close(code=4401)`` (custom application code) before
``accept()`` is called.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis as AsyncRedis

from app.core.config import get_settings
from app.core.security import decode_access_token
from app.services.websocket import EVENT_CHANNEL

logger = structlog.get_logger(__name__)

router = APIRouter()

__all__ = [
    "ConnectionManager",
    "event_consumer_loop",
    "get_connection_manager",
    "router",
]


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """In-process registry of live ``WebSocket`` clients keyed by user id.

    All mutation goes through ``_lock`` so concurrent ``connect`` /
    ``disconnect`` and the consumer's ``send_to_user`` / ``broadcast`` see a
    consistent view of the registry. Send failures are logged and the
    affected socket left for the endpoint loop to clean up via the normal
    disconnect path — we never decide that a socket is dead just because
    one ``send_json`` raised.
    """

    def __init__(self) -> None:
        """Empty registry; sockets are added by ``connect``."""
        self._connections: dict[uuid.UUID, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: uuid.UUID, websocket: WebSocket) -> None:
        """Register *websocket* under *user_id* (multi-tab supported)."""
        async with self._lock:
            self._connections.setdefault(user_id, set()).add(websocket)

    async def disconnect(self, user_id: uuid.UUID, websocket: WebSocket) -> None:
        """Drop *websocket* from *user_id*'s session set; clean up empty users."""
        async with self._lock:
            sockets = self._connections.get(user_id)
            if sockets is None:
                return
            sockets.discard(websocket)
            if not sockets:
                del self._connections[user_id]

    async def send_to_user(self, user_id: uuid.UUID, message: dict[str, Any]) -> int:
        """Send *message* to every active session of *user_id*; return delivered count."""
        async with self._lock:
            targets = list(self._connections.get(user_id, set()))
        return await self._send_all(targets, message)

    async def broadcast(self, message: dict[str, Any]) -> int:
        """Send *message* to every connected client; return delivered count."""
        async with self._lock:
            targets = [s for sockets in self._connections.values() for s in sockets]
        return await self._send_all(targets, message)

    async def _send_all(self, sockets: list[WebSocket], message: dict[str, Any]) -> int:
        delivered = 0
        for ws in sockets:
            try:
                await ws.send_json(message)
                delivered += 1
            except Exception as exc:
                # One dead client (TCP-reset OSError, mid-handshake
                # WebSocketDisconnect, app-state RuntimeError, etc.) must
                # never starve the rest of the broadcast — log and move on.
                logger.warning("websocket.send_failed", error=str(exc))
        return delivered


_manager = ConnectionManager()


def get_connection_manager() -> ConnectionManager:
    """Module-level accessor — tests can monkeypatch to inject a fresh manager."""
    return _manager


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(..., description="JWT access token"),
) -> None:
    """Authenticate via the ``?token=`` query param, then keep the socket open.

    The channel is server-driven — clients aren't expected to send anything,
    but ``receive_text()`` blocks until a message arrives or the peer
    disconnects, which is exactly the lifecycle we want.
    """
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload.sub)
    except Exception as exc:
        logger.warning("websocket.auth_failed", error=str(exc))
        # Application close codes 4000-4999 are reserved for app use; 4401
        # mirrors the HTTP 401 semantic for clients that introspect the code.
        await websocket.close(code=4401)
        return

    await websocket.accept()
    manager = get_connection_manager()
    await manager.connect(user_id, websocket)
    logger.info("websocket.connected", user_id=str(user_id))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(user_id, websocket)
        logger.info("websocket.disconnected", user_id=str(user_id))


# ---------------------------------------------------------------------------
# Redis subscriber — bridge from worker publishes to local sockets
# ---------------------------------------------------------------------------


async def _handle_event(raw_data: str) -> None:
    """Decode a single envelope and dispatch it through the manager."""
    try:
        envelope = json.loads(raw_data)
    except json.JSONDecodeError:
        logger.warning("websocket.consumer.bad_payload", data=raw_data)
        return

    kind = envelope.get("kind")
    payload = envelope.get("payload", {})
    manager = get_connection_manager()

    if kind == "broadcast":
        await manager.broadcast(payload)
    elif kind == "notify_user":
        try:
            target = uuid.UUID(envelope["user_id"])
        except (KeyError, ValueError):
            logger.warning("websocket.consumer.bad_user_id", envelope=envelope)
            return
        await manager.send_to_user(target, payload)
    else:
        logger.warning("websocket.consumer.unknown_kind", kind=kind)


async def event_consumer_loop() -> None:
    """Subscribe to the events channel and fan-out into the local manager.

    Runs as a background ``asyncio.Task`` for the FastAPI app's lifespan;
    cancelled cleanly on shutdown. Connection / decode errors are logged
    but never crash the loop — losing a single message is preferable to
    bringing the whole notification path down.
    """
    settings = get_settings()
    redis: AsyncRedis | None = None
    pubsub: Any = None
    try:
        redis = AsyncRedis.from_url(str(settings.REDIS_URL), decode_responses=True)
        pubsub = redis.pubsub()
        await pubsub.subscribe(EVENT_CHANNEL)
        logger.info("websocket.consumer.started", channel=EVENT_CHANNEL)
        async for raw in pubsub.listen():
            if raw.get("type") != "message":
                continue
            await _handle_event(raw["data"])
    except asyncio.CancelledError:
        logger.info("websocket.consumer.cancelled")
        raise
    except Exception as exc:
        # Log and exit — the lifespan will not restart us. In production
        # missing-Redis is an alert-worthy condition; in tests without a
        # live Redis we just stay quiet.
        logger.warning("websocket.consumer.failed", error=str(exc))
    finally:
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(EVENT_CHANNEL)
                await pubsub.aclose()
        if redis is not None:
            with contextlib.suppress(Exception):
                await redis.aclose()
