"""Tests for ``app.services.schedule_queue``.

The producer-side helpers — ``enqueue_compound`` (Phase 2) and
``cancel_compound`` (Phase 3) — wrap a thin layer of Redis I/O on top of
``schedule:pending_ops`` (sorted set) + ``schedule:pending_ops:by_compound_id``
(hash secondary index). Tests run against the session-wide ``redis_container``
from the root conftest so the helpers exercise real Redis semantics
(ZADD ordering, HSET return, ZREM races, etc.) instead of a hand-rolled
in-memory fake. Celery dispatch + WebSocket pub/sub are still mocked
because there's no broker / consumer pair in-process.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from app.schemas.schedule import ScheduleCompoundRequest, ScheduleOpInCompound
from app.services.schedule_queue import (
    BY_COMPOUND_ID_KEY,
    CancelResult,
    cancel_compound,
    enqueue_compound,
)
from app.services.scheduling import PENDING_OPS_KEY
from redis import Redis


def _make_compound(*, compound_id: uuid.UUID | None = None) -> ScheduleCompoundRequest:
    ops = [
        ScheduleOpInCompound(
            op="add",
            order_id=uuid.uuid4(),
            order_number="ORD-T",
            wafer_quantity=100,
            deadline="2026-07-01",
        ),
    ]
    return ScheduleCompoundRequest(
        compound_id=compound_id or uuid.uuid4(),
        group="grow",
        op_count=len(ops),
        ops=ops,
        requested_by=uuid.uuid4(),
    )


def _patch_taskdispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, MagicMock]:
    """Swap out the celery dispatch + websocket publish stubs.

    Returns ``(send_task_mock, notify_user_mock)``. Redis itself is NOT
    patched — the session-scoped ``redis_container`` fixture supplies a
    real Redis at ``settings.REDIS_URL``.
    """
    send_mock = MagicMock()
    monkeypatch.setattr("app.services.schedule_queue._send_run_task", send_mock)
    notify_mock = MagicMock()
    monkeypatch.setattr("app.services.schedule_queue.websocket.notify_user", notify_mock)
    return send_mock, notify_mock


# ---------------------------------------------------------------------------
# enqueue_compound
# ---------------------------------------------------------------------------


def test_enqueue_compound_adds_to_sorted_set_and_index(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """enqueue_compound writes one member to ``schedule:pending_ops`` and
    one entry to ``schedule:pending_ops:by_compound_id`` keyed by
    compound_id. Also fires ``send_task`` when status is idle.
    """
    send_mock, _ = _patch_taskdispatch(monkeypatch)
    compound = _make_compound()

    enqueue_compound(compound)

    assert redis_client.zcard(PENDING_OPS_KEY) == 1
    indexed = redis_client.hget(BY_COMPOUND_ID_KEY, str(compound.compound_id))
    assert indexed is not None
    parsed = json.loads(indexed)
    assert parsed["compound_id"] == str(compound.compound_id)
    assert parsed["group"] == "grow"
    assert "_seq" in parsed
    # Worker not running → send_task fired.
    send_mock.assert_called_once()


def test_enqueue_compound_skips_send_task_when_running(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """If ``schedule:status`` says ``running``, the in-flight task will
    auto-retrigger; ``enqueue_compound`` shouldn't fire another celery
    dispatch.
    """
    send_mock, _ = _patch_taskdispatch(monkeypatch)
    redis_client.set("schedule:status", json.dumps({"state": "running"}))

    enqueue_compound(_make_compound())

    assert send_mock.call_count == 0


# ---------------------------------------------------------------------------
# cancel_compound
# ---------------------------------------------------------------------------


def test_cancel_compound_removes_from_queue_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Happy path: compound in queue → ZREM + HDEL + notify_user."""
    _, notify_mock = _patch_taskdispatch(monkeypatch)
    compound = _make_compound()
    enqueue_compound(compound)

    result = cancel_compound(compound.compound_id)

    assert result is CancelResult.cancelled
    assert redis_client.zcard(PENDING_OPS_KEY) == 0
    assert redis_client.hget(BY_COMPOUND_ID_KEY, str(compound.compound_id)) is None
    notify_mock.assert_called_once()
    msg = notify_mock.call_args.kwargs["message"]
    assert msg["type"] == "schedule.compound_cancelled"
    assert msg["compound_id"] == str(compound.compound_id)


def test_cancel_compound_returns_in_progress_when_index_stale(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """If the secondary index has a member string but the sorted set
    doesn't (= worker popped between our HGET and ZREM, but hasn't yet
    cleaned the index), return ``in_progress``. Also cleans up the stale
    index entry as a side effect.
    """
    _, notify_mock = _patch_taskdispatch(monkeypatch)
    compound_id = uuid.uuid4()
    # Plant a stale index entry without a matching sorted-set member.
    stale_member = json.dumps({"compound_id": str(compound_id), "requested_by": str(uuid.uuid4())})
    redis_client.hset(BY_COMPOUND_ID_KEY, str(compound_id), stale_member)

    result = cancel_compound(compound_id)

    assert result is CancelResult.in_progress
    # Stale entry was cleaned anyway.
    assert redis_client.hget(BY_COMPOUND_ID_KEY, str(compound_id)) is None
    # No notify on race-loss — the worker will surface outcome via the
    # normal ``schedule.updated`` / ``schedule.compound_failed`` path.
    notify_mock.assert_not_called()


def test_cancel_compound_returns_not_found_when_id_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No index entry at all → ``not_found``. Means the compound either
    was never enqueued, or was processed long enough ago that the worker
    already cleaned the index entry.
    """
    _, notify_mock = _patch_taskdispatch(monkeypatch)

    result = cancel_compound(uuid.uuid4())

    assert result is CancelResult.not_found
    notify_mock.assert_not_called()
