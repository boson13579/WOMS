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
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import decode_access_token
from app.repositories import user as user_repo
from app.services.websocket import EVENT_CHANNEL

logger = structlog.get_logger(__name__)

router = APIRouter()

WS_CLOSE_AUTH_FAILED: int = 4401
WS_CLOSE_DB_ERROR: int = 4500

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
    token: str | None = Query(None, description="JWT access token (optional; cookie fallback)"),
    db: Session = Depends(get_db),
) -> None:
    """Authenticate via ``?token=`` query param or ``access_token`` cookie.

    Priority: bearer query param → cookie. Both absent → close(4401).
    The DB session is closed immediately after the auth lookup via
    ``db.close()``, so no pool slot is held during the long-running loop.
    FastAPI's ``get_db`` generator calls ``close()`` again on handler exit,
    which is a no-op once the session is already closed.
    The channel is server-driven; ``receive_text()`` blocks until the peer
    disconnects, which is exactly the lifecycle we want.
    """
    actual_token = token or websocket.cookies.get("access_token")
    if actual_token is None:
        await websocket.close(code=WS_CLOSE_AUTH_FAILED)
        return
    try:
        payload = decode_access_token(actual_token)
        user_id = uuid.UUID(payload.sub)
        user = user_repo.get_by_id(db, user_id)
        if user is None or not user.is_active:
            raise ValueError("inactive or missing user")
    except SQLAlchemyError as exc:
        # DB errors (pool exhausted, query timeout, etc.) are server-side
        # faults, not auth failures — log at ERROR so monitoring fires.
        logger.error("websocket.db_error", error=str(exc), exc_info=True)
        await websocket.close(code=WS_CLOSE_DB_ERROR)
        return
    except Exception as exc:
        logger.warning("websocket.auth_failed", error=str(exc))
        # Application close codes 4000-4999 are reserved for app use; 4401
        # mirrors the HTTP 401 semantic for clients that introspect the code.
        await websocket.close(code=WS_CLOSE_AUTH_FAILED)
        return
    finally:
        # Release the DB connection back to the pool before the long-running
        # loop — the while loop doesn't use the DB at all.
        db.close()

    await websocket.accept()
    manager = get_connection_manager()
    logger.info("websocket.connected", user_id=str(user_id))

    try:
        await manager.connect(user_id, websocket)
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
    cancelled cleanly on shutdown. Per-message decode errors stay confined
    to ``_handle_event`` (logged + dropped). A *loop-level* error — failed
    subscribe, broken pubsub iterator, Redis connection death — is
    **terminal**: the loop exits and the lifespan does not restart it, so
    every subsequent ``broadcast`` / ``notify_user`` becomes a silent no-op
    until the FastAPI process is restarted.

    For that reason a terminal failure is logged at ``ERROR`` (with
    ``exc_info``) — operator-grade so log-based alerting fires. Auto-restart
    isn't built in here on purpose: in tests without a live Redis the
    consumer would loop forever burning CPU, and the right operational
    response is "restart the pod" so a healthy lifespan re-runs.
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
        # Terminal — see docstring. ERROR + exc_info so monitoring picks it
        # up; without this every WS notification silently disappears for
        # the rest of the process's life.
        logger.error(
            "websocket.consumer.failed",
            error=str(exc),
            exc_info=True,
        )
    finally:
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(EVENT_CHANNEL)
                await pubsub.aclose()
        if redis is not None:
            with contextlib.suppress(Exception):
                await redis.aclose()
