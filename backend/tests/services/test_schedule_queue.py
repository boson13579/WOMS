"""Tests for ``app.services.schedule_queue``.

The producer-side helpers — ``enqueue_compound`` (Phase 2) and
``cancel_compound`` (Phase 3) — wrap a thin layer of Redis I/O on top of
``schedule:pending_ops`` (sorted set) + ``schedule:pending_ops:by_compound_id``
(hash secondary index). These tests use a small in-memory fake to verify
the contract without spinning up a real Redis.
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


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of Redis these helpers use."""

    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._zsets: dict[str, list[tuple[float, str]]] = {}
        self._hashes: dict[str, dict[str, str]] = {}

    def get(self, key: str) -> str | None:
        return self._strings.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._strings[key] = value

    def incr(self, key: str) -> int:
        cur = int(self._strings.get(key, "0")) + 1
        self._strings[key] = str(cur)
        return cur

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        bucket = self._zsets.setdefault(key, [])
        added = 0
        for member, score in mapping.items():
            existing = next((i for i, (_, m) in enumerate(bucket) if m == member), None)
            if existing is not None:
                bucket.pop(existing)
            else:
                added += 1
            bucket.append((score, member))
        bucket.sort()
        return added

    def zrem(self, key: str, *members: str) -> int:
        bucket = self._zsets.get(key)
        if not bucket:
            return 0
        removed = 0
        new_bucket = []
        wanted = set(members)
        for score, member in bucket:
            if member in wanted:
                removed += 1
            else:
                new_bucket.append((score, member))
        self._zsets[key] = new_bucket
        return removed

    def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, []))

    def hset(self, key: str, field: str, value: str) -> int:
        bucket = self._hashes.setdefault(key, {})
        is_new = field not in bucket
        bucket[field] = value
        return 1 if is_new else 0

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hdel(self, key: str, *fields: str) -> int:
        bucket = self._hashes.get(key)
        if bucket is None:
            return 0
        removed = 0
        for f in fields:
            if bucket.pop(f, None) is not None:
                removed += 1
        return removed


def _make_compound(
    *, compound_id: uuid.UUID | None = None
) -> ScheduleCompoundRequest:
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


def _patch_redis_and_taskdispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeRedis, MagicMock, MagicMock]:
    """Swap out the helpers' module-level Redis client + celery dispatch.

    Returns ``(fake_redis, send_task_mock, notify_user_mock)``. The
    helper-level monkeypatch makes the tests independent of the running
    Celery / Redis stack.
    """
    fake = _FakeRedis()
    monkeypatch.setattr("app.services.schedule_queue._redis", lambda: fake)
    send_mock = MagicMock()
    monkeypatch.setattr("app.services.schedule_queue._send_run_task", send_mock)
    notify_mock = MagicMock()
    monkeypatch.setattr("app.services.schedule_queue.websocket.notify_user", notify_mock)
    return fake, send_mock, notify_mock


# ---------------------------------------------------------------------------
# enqueue_compound
# ---------------------------------------------------------------------------


def test_enqueue_compound_adds_to_sorted_set_and_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enqueue_compound writes one member to ``schedule:pending_ops`` and
    one entry to ``schedule:pending_ops:by_compound_id`` keyed by
    compound_id. Also fires ``send_task`` when status is idle.
    """
    fake, send_mock, _ = _patch_redis_and_taskdispatch(monkeypatch)
    compound = _make_compound()

    enqueue_compound(compound)

    assert fake.zcard(PENDING_OPS_KEY) == 1
    indexed = fake.hget(BY_COMPOUND_ID_KEY, str(compound.compound_id))
    assert indexed is not None
    parsed = json.loads(indexed)
    assert parsed["compound_id"] == str(compound.compound_id)
    assert parsed["group"] == "grow"
    assert "_seq" in parsed
    # Worker not running → send_task fired.
    send_mock.assert_called_once()


def test_enqueue_compound_skips_send_task_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``schedule:status`` says ``running``, the in-flight task will
    auto-retrigger; ``enqueue_compound`` shouldn't fire another celery
    dispatch.
    """
    fake, send_mock, _ = _patch_redis_and_taskdispatch(monkeypatch)
    fake.set("schedule:status", json.dumps({"state": "running"}))

    enqueue_compound(_make_compound())

    assert send_mock.call_count == 0


# ---------------------------------------------------------------------------
# cancel_compound
# ---------------------------------------------------------------------------


def test_cancel_compound_removes_from_queue_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: compound in queue → ZREM + HDEL + notify_user."""
    fake, _, notify_mock = _patch_redis_and_taskdispatch(monkeypatch)
    compound = _make_compound()
    enqueue_compound(compound)

    result = cancel_compound(compound.compound_id)

    assert result is CancelResult.cancelled
    assert fake.zcard(PENDING_OPS_KEY) == 0
    assert fake.hget(BY_COMPOUND_ID_KEY, str(compound.compound_id)) is None
    notify_mock.assert_called_once()
    msg = notify_mock.call_args.kwargs["message"]
    assert msg["type"] == "schedule.compound_cancelled"
    assert msg["compound_id"] == str(compound.compound_id)


def test_cancel_compound_returns_in_progress_when_index_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the secondary index has a member string but the sorted set
    doesn't (= worker popped between our HGET and ZREM, but hasn't yet
    cleaned the index), return ``in_progress``. Also cleans up the stale
    index entry as a side effect.
    """
    fake, _, notify_mock = _patch_redis_and_taskdispatch(monkeypatch)
    compound_id = uuid.uuid4()
    # Plant a stale index entry without a matching sorted-set member.
    stale_member = json.dumps(
        {"compound_id": str(compound_id), "requested_by": str(uuid.uuid4())}
    )
    fake.hset(BY_COMPOUND_ID_KEY, str(compound_id), stale_member)

    result = cancel_compound(compound_id)

    assert result is CancelResult.in_progress
    # Stale entry was cleaned anyway.
    assert fake.hget(BY_COMPOUND_ID_KEY, str(compound_id)) is None
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
    _, _, notify_mock = _patch_redis_and_taskdispatch(monkeypatch)

    result = cancel_compound(uuid.uuid4())

    assert result is CancelResult.not_found
    notify_mock.assert_not_called()
