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
from datetime import date
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


# ---------------------------------------------------------------------------
# cancel_compound — db_action compensation (the producer-pre-write fix)
# ---------------------------------------------------------------------------
#
# Bug being fixed here: ``cancel_compound`` previously only ZREM'd the
# compound from the Redis queue. It didn't touch the DB. Producers
# (create_order / update_order / delete_order) had already written
# ``is_processing_locked=True`` (and, for create, an entire row) at
# enqueue time, expecting the worker to finalize the DB side on
# accept/reject. Cancelling removed the compound before the worker saw
# it, leaving the producer's pre-write stranded:
#
#  * Create: orphan row with is_processing_locked=True forever
#  * Update: status pinned to ``pending``, lock True forever
#  * Delete: lock True forever
#
# Fix: ``cancel_compound`` now runs ``perform_compound_db_action(
# accepted=False)`` — same compensation the worker would run on a
# rejection. These tests pin that behavior down.


def _build_compound_with_db_action(
    *,
    kind: str,
    order_id: uuid.UUID,
    actor_id: uuid.UUID,
    op: str = "add",
) -> ScheduleCompoundRequest:
    """Helper that produces a compound shape matching what the producer
    functions in ``services/order.py`` actually emit — i.e., with a
    ``db_action`` payload of the requested ``kind``. The cancel-compensation
    branches in ``perform_compound_db_action`` only fire when the payload
    is present, so ``_make_compound`` (which omits db_action) wouldn't
    exercise them.
    """
    from app.schemas.schedule import CompoundDbAction

    ops = [
        ScheduleOpInCompound(
            op=op,  # type: ignore[arg-type]
            order_id=order_id,
            order_number="ORD-CANCEL-TEST",
            wafer_quantity=100,
            deadline="2026-07-01",
        ),
    ]
    return ScheduleCompoundRequest(
        compound_id=uuid.uuid4(),
        group="grow" if op == "add" else "shrink",
        op_count=len(ops),
        ops=ops,
        requested_by=actor_id,
        db_action=CompoundDbAction(
            kind=kind,  # type: ignore[arg-type]
            actor_id=actor_id,
            new_wafer_quantity=200,
            new_requested_delivery_date="2026-08-15",
            old_wafer_quantity=100,
            old_requested_delivery_date="2026-07-01",
        ),
    )


def _seed_user_and_order(
    db_session,  # type: ignore[no-untyped-def]
    *,
    is_processing_locked: bool,
    status,  # type: ignore[no-untyped-def]
    scheduled_production_date=None,  # type: ignore[no-untyped-def]
):
    """Create one actor + one Order row that mirrors what a producer
    function leaves behind right before enqueueing a compound.
    """
    import bcrypt
    from app.models.order import Order
    from app.models.user import User, UserRole

    actor = User(
        username=f"cancel-actor-{uuid.uuid4().hex[:6]}",
        email=f"cancel-actor-{uuid.uuid4().hex[:6]}@test.internal",
        password_hash=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        role=UserRole.scheduler,
        is_active=True,
    )
    db_session.add(actor)
    db_session.commit()
    order = Order(
        order_number=f"ORD-CANCEL-{uuid.uuid4().hex[:6]}",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 7, 1),
        created_by=actor.id,
        status=status,
        is_processing_locked=is_processing_locked,
        scheduled_production_date=scheduled_production_date,
    )
    db_session.add(order)
    db_session.commit()
    return actor, order


def _patch_compound_finalize_sessionlocal(
    monkeypatch: pytest.MonkeyPatch,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    """Route ``compound_finalize.SessionLocal()`` to the test session so
    the cancel path's DB writes commit into the same transaction the
    test inspects (and get rolled back per-test).
    """

    class _NonClosingSession:
        def __init__(self, inner):  # type: ignore[no-untyped-def]
            self._inner = inner

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(self._inner, name)

        def close(self):  # type: ignore[no-untyped-def]
            pass  # outer fixture rolls back

    monkeypatch.setattr(
        "app.services.compound_finalize.SessionLocal",
        lambda: _NonClosingSession(db_session),
    )


def test_cancel_compound_create_kind_soft_deletes_orphan_row(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    """Cancelling a ``create`` compound must clean up the orphan row that
    the producer pre-INSERTed. Without this, ``create_order`` →
    ``cancel`` leaves the row visible (is_deleted=False) and stuck at
    ``is_processing_locked=True``, with no recovery path.
    """
    from app.models.order import Order, OrderStatus

    _, _ = _patch_taskdispatch(monkeypatch)
    _patch_compound_finalize_sessionlocal(monkeypatch, db_session)

    actor, order = _seed_user_and_order(
        db_session,
        is_processing_locked=True,
        status=OrderStatus.pending,
    )
    compound = _build_compound_with_db_action(
        kind="create",
        order_id=order.id,
        actor_id=actor.id,
        op="add",
    )
    enqueue_compound(compound)

    result = cancel_compound(compound.compound_id)
    assert result is CancelResult.cancelled

    db_session.expire_all()
    refreshed = db_session.get(Order, order.id)
    assert refreshed is not None
    assert refreshed.is_deleted is True, "orphan row should be soft-deleted"
    assert refreshed.status == OrderStatus.cancelled
    assert refreshed.is_processing_locked is False


def test_cancel_compound_update_kind_clears_lock_and_restores_status(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    """Cancelling an ``update`` compound must release the in-flight lock
    AND snap status back. If the row had been scheduled before the PATCH
    (= producer set status=pending but ``scheduled_production_date`` is
    still populated), status restores to ``scheduled``. Otherwise
    ``pending``.

    Without this fix the row would stay ``status=pending,
    is_processing_locked=True`` forever, even though the user explicitly
    retracted their PATCH.
    """
    from app.models.order import Order, OrderStatus

    _, _ = _patch_taskdispatch(monkeypatch)
    _patch_compound_finalize_sessionlocal(monkeypatch, db_session)

    actor, order = _seed_user_and_order(
        db_session,
        is_processing_locked=True,
        status=OrderStatus.pending,
        scheduled_production_date=date(2026, 7, 5),
    )
    compound = _build_compound_with_db_action(
        kind="update",
        order_id=order.id,
        actor_id=actor.id,
        op="add",
    )
    enqueue_compound(compound)

    result = cancel_compound(compound.compound_id)
    assert result is CancelResult.cancelled

    db_session.expire_all()
    refreshed = db_session.get(Order, order.id)
    assert refreshed is not None
    assert refreshed.is_processing_locked is False, "lock must be cleared"
    # Had scheduled_production_date → restored to scheduled.
    assert refreshed.status == OrderStatus.scheduled
    # Business columns untouched (producer never wrote them either).
    assert refreshed.wafer_quantity == 100
    assert refreshed.requested_delivery_date == date(2026, 7, 1)


def test_cancel_compound_delete_kind_clears_lock_and_keeps_row_alive(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    """Cancelling a ``delete`` compound undoes the producer's lock-claim
    and leaves the row alive (``is_deleted=False``). The user changed
    their mind about deleting — the order stays in the system.
    """
    from app.models.order import Order, OrderStatus

    _, _ = _patch_taskdispatch(monkeypatch)
    _patch_compound_finalize_sessionlocal(monkeypatch, db_session)

    actor, order = _seed_user_and_order(
        db_session,
        is_processing_locked=True,
        status=OrderStatus.scheduled,
        scheduled_production_date=date(2026, 7, 5),
    )
    compound = _build_compound_with_db_action(
        kind="delete",
        order_id=order.id,
        actor_id=actor.id,
        op="remove",
    )
    enqueue_compound(compound)

    result = cancel_compound(compound.compound_id)
    assert result is CancelResult.cancelled

    db_session.expire_all()
    refreshed = db_session.get(Order, order.id)
    assert refreshed is not None
    assert refreshed.is_deleted is False, "delete cancel must NOT soft-delete the row"
    assert refreshed.is_processing_locked is False
    assert refreshed.status == OrderStatus.scheduled


def test_cancel_compound_in_progress_race_skips_db_compensation(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    """If the worker already ZPOPMIN'd the compound before our ZREM, we
    return ``in_progress`` and must NOT compensate the DB — the worker
    will run accept-or-reject db_action itself. Double-compensating would
    flip a freshly-accepted row into deleted+cancelled state mid-flight.
    """
    from app.models.order import Order, OrderStatus

    _, _ = _patch_taskdispatch(monkeypatch)
    _patch_compound_finalize_sessionlocal(monkeypatch, db_session)

    actor, order = _seed_user_and_order(
        db_session,
        is_processing_locked=True,
        status=OrderStatus.pending,
    )
    compound_id = uuid.uuid4()
    # Plant a stale index entry without a matching sorted-set member —
    # simulates worker having popped between our HGET and ZREM.
    stale_member = json.dumps(
        {
            "compound_id": str(compound_id),
            "requested_by": str(actor.id),
            "ops": [{"order_id": str(order.id)}],
            "db_action": {"kind": "create", "actor_id": str(actor.id)},
        }
    )
    redis_client.hset(BY_COMPOUND_ID_KEY, str(compound_id), stale_member)

    result = cancel_compound(compound_id)
    assert result is CancelResult.in_progress

    # Critical: row is unchanged from its producer-set state. The worker
    # owns the db_action for this compound; our cancel path bailed cleanly.
    db_session.expire_all()
    refreshed = db_session.get(Order, order.id)
    assert refreshed is not None
    assert refreshed.is_deleted is False
    assert refreshed.is_processing_locked is True
    assert refreshed.status == OrderStatus.pending
