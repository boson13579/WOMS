"""Tests for the WebSocket endpoint, ConnectionManager, and event handler.

Coverage strategy:

- :class:`ConnectionManager` — pure async unit tests with ``AsyncMock``
  WebSockets. No FastAPI involved.
- ``_handle_event`` — feed JSON envelopes, verify the right manager
  method was called.
- ``websocket_endpoint`` — TestClient ``websocket_connect`` for happy
  path + auth-rejection path.

The Redis-bridge ``event_consumer_loop`` is not exercised end-to-end here —
that requires a live Redis pub/sub which isn't part of our unit budget.
The publisher (``services/websocket``) and ``_handle_event`` cover the two
halves separately.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import bcrypt
import pytest
from app.api.v1 import websocket as ws_api
from app.api.v1.websocket import ConnectionManager, _handle_event
from app.core.security import create_access_token
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

# ---------------------------------------------------------------------------
# Module-level helpers (CLAUDE.md: no fixtures)
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    username: str,
    role: UserRole = UserRole.viewer,
) -> User:
    user = User(
        username=username,
        email=f"{username}@test.internal",
        password_hash=bcrypt.hashpw(b"password123", bcrypt.gensalt()).decode(),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# ConnectionManager — pure async unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_connects_and_routes_send_to_user_only_to_target() -> None:
    manager = ConnectionManager()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    a_socket1 = AsyncMock()
    a_socket2 = AsyncMock()
    b_socket = AsyncMock()

    await manager.connect(user_a, a_socket1)
    await manager.connect(user_a, a_socket2)
    await manager.connect(user_b, b_socket)

    delivered = await manager.send_to_user(user_a, {"hello": "a"})

    assert delivered == 2
    a_socket1.send_json.assert_awaited_once_with({"hello": "a"})
    a_socket2.send_json.assert_awaited_once_with({"hello": "a"})
    b_socket.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_manager_broadcast_hits_every_connection() -> None:
    manager = ConnectionManager()
    sockets = [AsyncMock() for _ in range(3)]
    for s in sockets:
        await manager.connect(uuid.uuid4(), s)

    delivered = await manager.broadcast({"type": "schedule.updated"})

    assert delivered == 3
    for s in sockets:
        s.send_json.assert_awaited_once_with({"type": "schedule.updated"})


@pytest.mark.asyncio
async def test_manager_disconnect_removes_socket_and_cleans_empty_user() -> None:
    manager = ConnectionManager()
    user_id = uuid.uuid4()
    sock = AsyncMock()
    await manager.connect(user_id, sock)

    await manager.disconnect(user_id, sock)

    # User key cleaned out once last socket is gone.
    assert user_id not in manager._connections
    # Subsequent disconnect on already-gone user is a no-op.
    await manager.disconnect(user_id, sock)


@pytest.mark.asyncio
async def test_manager_send_failure_does_not_remove_socket() -> None:
    """A single failed send_json mustn't take the connection out of the
    registry — only the endpoint loop's WebSocketDisconnect handler does."""
    manager = ConnectionManager()
    user_id = uuid.uuid4()
    bad = AsyncMock()
    bad.send_json.side_effect = RuntimeError("transport closed")
    await manager.connect(user_id, bad)

    delivered = await manager.send_to_user(user_id, {"x": 1})

    assert delivered == 0
    assert bad in manager._connections[user_id]


@pytest.mark.asyncio
async def test_broadcast_continues_past_unexpected_exception() -> None:
    """One dead client (TCP-reset OSError, unknown library exception, etc.)
    must NOT abort the broadcast — every other connected socket has to
    receive the notification.

    Pre-fix the loop only caught ``WebSocketDisconnect`` / ``RuntimeError``;
    a peer disconnect surfaced as ``OSError`` would propagate, the
    enumeration would unwind, and every client after the bad one in the
    iteration order silently lost the message.
    """
    manager = ConnectionManager()
    bad = AsyncMock()
    bad.send_json.side_effect = OSError("connection reset by peer")
    good_a = AsyncMock()
    good_b = AsyncMock()
    await manager.connect(uuid.uuid4(), bad)
    await manager.connect(uuid.uuid4(), good_a)
    await manager.connect(uuid.uuid4(), good_b)

    delivered = await manager.broadcast({"type": "schedule.updated"})

    # Two of three deliveries succeeded; the bad socket logged-and-skipped.
    assert delivered == 2
    good_a.send_json.assert_awaited_once_with({"type": "schedule.updated"})
    good_b.send_json.assert_awaited_once_with({"type": "schedule.updated"})


# ---------------------------------------------------------------------------
# _handle_event — envelope decoding + dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_event_dispatches_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ConnectionManager()
    monkeypatch.setattr(ws_api, "_manager", manager)
    sock = AsyncMock()
    await manager.connect(uuid.uuid4(), sock)

    raw = json.dumps({"kind": "broadcast", "payload": {"type": "schedule.updated"}})
    await _handle_event(raw)

    sock.send_json.assert_awaited_once_with({"type": "schedule.updated"})


@pytest.mark.asyncio
async def test_handle_event_dispatches_notify_user(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ConnectionManager()
    monkeypatch.setattr(ws_api, "_manager", manager)
    user_id = uuid.uuid4()
    sock = AsyncMock()
    other_sock = AsyncMock()
    await manager.connect(user_id, sock)
    await manager.connect(uuid.uuid4(), other_sock)

    raw = json.dumps(
        {
            "kind": "notify_user",
            "user_id": str(user_id),
            "payload": {"type": "schedule.add_failed", "reason": "capacity_exceeded"},
        }
    )
    await _handle_event(raw)

    sock.send_json.assert_awaited_once()
    other_sock.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_event_drops_malformed_json() -> None:
    # Should log and return; the assertion is "no exception raised".
    await _handle_event("not json")


@pytest.mark.asyncio
async def test_handle_event_drops_unknown_kind() -> None:
    await _handle_event(json.dumps({"kind": "weird", "payload": {}}))


# ---------------------------------------------------------------------------
# WebSocket endpoint — TestClient round-trip
# ---------------------------------------------------------------------------


def test_websocket_connects_with_valid_token(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="ws_ok", role=UserRole.viewer)
    token = create_access_token(user.id, user.role)

    with client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
        # Connection accepted = test passes. Disconnect cleanly via context exit.
        ws.close()


def test_websocket_rejects_invalid_token(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/ws?token=not-a-real-jwt") as ws:
            ws.receive_text()
    # 4401 is our application-level "auth failed" close code.
    assert exc_info.value.code == 4401


def test_websocket_rejects_missing_token(client: TestClient) -> None:
    # No token query param and no cookie → endpoint closes with 4401.
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.receive_text()
    assert exc_info.value.code == 4401


def test_websocket_accepts_cookie_auth(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="ws_cookie_user", role=UserRole.viewer)
    token = create_access_token(user.id, user.role)
    client.cookies.set("access_token", token)
    try:
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.close()
    finally:
        client.cookies.clear()


def test_websocket_rejects_inactive_user(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="ws_inactive_user", role=UserRole.viewer)
    user.is_active = False
    db_session.commit()
    token = create_access_token(user.id, user.role)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/ws?token={token}") as ws:
            ws.receive_text()
    assert exc_info.value.code == 4401
