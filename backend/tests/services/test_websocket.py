"""Unit tests for the synchronous publisher in ``app.services.websocket``.

The publisher's only job is to ``PUBLISH`` a JSON envelope on the
events channel. We mock the Redis client and inspect what got pushed.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from app.services import websocket as ws_service


def test_broadcast_publishes_envelope_with_kind_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()
    monkeypatch.setattr(ws_service, "_redis", lambda: fake)

    ws_service.broadcast({"type": "schedule.updated"})

    fake.publish.assert_called_once()
    channel, raw = fake.publish.call_args.args
    assert channel == ws_service.EVENT_CHANNEL
    envelope = json.loads(raw)
    assert envelope == {
        "kind": "broadcast",
        "payload": {"type": "schedule.updated"},
    }


def test_notify_user_publishes_envelope_with_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()
    monkeypatch.setattr(ws_service, "_redis", lambda: fake)

    user_id = uuid.uuid4()
    payload = {"type": "schedule.add_failed", "reason": "capacity_exceeded"}

    ws_service.notify_user(user_id=user_id, message=payload)

    fake.publish.assert_called_once()
    channel, raw = fake.publish.call_args.args
    assert channel == ws_service.EVENT_CHANNEL
    envelope = json.loads(raw)
    assert envelope == {
        "kind": "notify_user",
        "user_id": str(user_id),
        "payload": payload,
    }


def test_publisher_swallows_redis_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort delivery: a Redis outage must not propagate to the caller."""
    fake = MagicMock()
    fake.publish.side_effect = ConnectionError("redis down")
    monkeypatch.setattr(ws_service, "_redis", lambda: fake)

    # Both calls return None and don't raise.
    ws_service.broadcast({"type": "x"})
    ws_service.notify_user(user_id=uuid.uuid4(), message={"type": "y"})
