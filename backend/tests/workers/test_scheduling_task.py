"""Tests for ``app.workers.scheduling`` Celery tasks.

External collaborators (SQLAlchemy session, algorithm primitives, WebSocket
publish) are mocked at the worker module's binding site so the body under
test is exercised in isolation. **Redis is real** — the session-wide
``redis_container`` from the root conftest exposes a Redis 7 instance at
the URL the app reads from settings, and the autouse ``_redis_flushdb``
fixture wipes the keyspace between tests for isolation.

Tasks are invoked through ``.apply()``, which runs them synchronously while
still wiring up the ``self.request`` binding the worker code reads.
"""

from __future__ import annotations

import json
import types
import uuid
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from app.services.scheduling import (
    PENDING_OPS_KEY,
    STATE_KEY,
    STATUS_KEY,
    ScheduledResult,
    ScheduleResult,
    SchedulerState,
    SchedulingOrder,
    SkippedOrder,
)
from app.workers.scheduling import (
    advance_day_task,
    rebuild_schedule_task,
    run_scheduling_task,
)
from redis import Redis

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_leaf_op(
    *,
    op: str = "add",
    order_id: uuid.UUID | None = None,
    order_number: str = "ORD-T",
    wafer_quantity: int = 1000,
    deadline: str = "2026-05-07",
    fake_deadline: str | None = None,
) -> dict[str, Any]:
    """Build a leaf op dict (shape matches ``ScheduleOpInCompound`` minus its
    pydantic instance — JSON-equivalent).
    """
    payload: dict[str, Any] = {
        "op": op,
        "order_id": str(order_id or uuid.uuid4()),
        "order_number": order_number,
        "wafer_quantity": wafer_quantity,
        "deadline": deadline,
    }
    if fake_deadline is not None:
        payload["fake_deadline"] = fake_deadline
    return payload


def _make_compound(
    *,
    ops: list[dict[str, Any]] | None = None,
    group: str | None = None,
    requested_by: uuid.UUID | None = None,
    compound_id: uuid.UUID | None = None,
    op_count: int | None = None,
) -> dict[str, Any]:
    """Build a compound dict that ``_enqueue`` can land in the fake redis.

    Defaults to a single-add compound for the most common test case. When
    ``group`` isn't given, infer from the first op (``shrink`` for
    remove/unpin, ``grow`` for add/pin). ``op_count`` defaults to
    ``len(ops)`` but can be overridden to deliberately trip the
    worker-side mismatch guard in tests.
    """
    if not ops:
        ops = [_make_leaf_op()]
    if group is None:
        first = ops[0]["op"]
        group = "shrink" if first in ("remove", "unpin") else "grow"
    return {
        "compound_id": str(compound_id or uuid.uuid4()),
        "group": group,
        "op_count": op_count if op_count is not None else len(ops),
        "requested_by": str(requested_by or uuid.uuid4()),
        "ops": ops,
    }


def _make_op(
    *,
    op: str = "add",
    group: str | None = None,
    order_id: uuid.UUID | None = None,
    order_number: str = "ORD-T",
    wafer_quantity: int = 1000,
    deadline: str = "2026-05-07",
    fake_deadline: str | None = None,
    requested_by: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Backward-compat helper: build a 1-op compound from leaf-op kwargs.

    Most existing tests want "stick a single ``add`` (or ``pin`` etc.) into
    the queue". Under the compound model that's a 1-op compound. This
    wrapper preserves the old call sites verbatim — pass leaf-op kwargs,
    get a compound back. For multi-op compounds use ``_make_compound``
    explicitly.
    """
    leaf = _make_leaf_op(
        op=op,
        order_id=order_id,
        order_number=order_number,
        wafer_quantity=wafer_quantity,
        deadline=deadline,
        fake_deadline=fake_deadline,
    )
    return _make_compound(
        ops=[leaf],
        group=group,
        requested_by=requested_by,
    )


def _install_auto_retrigger_delay(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Wire ``run_scheduling_task.delay`` to synchronously call ``apply()``.

    Compound design: each task invocation handles ONE compound then
    ``.delay()``s itself if more remain. Tests want to call ``apply()``
    once and have the whole queue drain — so we route ``.delay()`` straight
    back into ``apply()`` here. A depth cap catches infinite-loop bugs.
    """
    delay_mock = MagicMock()

    def _side_effect() -> None:
        if delay_mock.call_count > 50:
            raise RuntimeError(f"run_scheduling_task re-triggered {delay_mock.call_count} times")
        run_scheduling_task.apply()

    delay_mock.side_effect = _side_effect
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", delay_mock)
    return delay_mock


def _enqueue(redis_client: Redis, compound: dict[str, Any]) -> None:
    """Enqueue *compound* into the live Redis sorted-set like the real producer.

    Mirrors ``schedule_queue.enqueue_compound``: bumps the seq counter,
    embeds it as ``_seq``, and ZADDs at the score computed by ``score_for_op``
    using the compound's group field.
    """
    from app.services.scheduling import (
        PENDING_OPS_SEQ_KEY,
        score_for_op,
    )

    group = compound["group"]
    seq = redis_client.incr(PENDING_OPS_SEQ_KEY)
    payload = {**compound, "_seq": seq}
    redis_client.zadd(PENDING_OPS_KEY, {json.dumps(payload): score_for_op(group=group, seq=seq)})


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    add_order: MagicMock | None = None,
    compute_schedule: Any | None = None,
) -> dict[str, MagicMock]:
    """Stub out the side-effecting collaborators of ``run_scheduling_task``.

    Redis itself is NOT patched — the session-scoped ``redis_container``
    fixture supplies a real client at ``settings.REDIS_URL``, so anything
    the worker reaches into via ``_get_redis()`` hits the live container.

    Returns a dict of the installed mocks so individual tests can make
    assertions on them.
    """
    add_mock = add_order or MagicMock(return_value=ScheduleResult(status="success"))
    monkeypatch.setattr("app.workers.scheduling.add_order", add_mock)

    remove_mock = MagicMock(return_value=ScheduleResult(status="success"))
    monkeypatch.setattr("app.workers.scheduling.remove_order", remove_mock)

    pin_mock = MagicMock(return_value=ScheduleResult(status="success"))
    monkeypatch.setattr("app.workers.scheduling.pin_order", pin_mock)

    unpin_mock = MagicMock(return_value=ScheduleResult(status="success"))
    monkeypatch.setattr("app.workers.scheduling.unpin_order", unpin_mock)

    compute_mock = compute_schedule or (lambda state: [])
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", compute_mock)

    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())

    apply_mock = MagicMock(return_value=0)
    monkeypatch.setattr("app.workers.scheduling.order_service.apply_schedule", apply_mock)

    broadcast_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", broadcast_mock)

    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)

    delay_mock = _install_auto_retrigger_delay(monkeypatch)

    # Phase 4: run_scheduling_task now also dispatches the slow materializer
    # task on each compound success. Tests that focus on the fast path
    # don't want the real materializer to run, so we mock its .delay().
    # The dedicated materialize tests reset this monkeypatch locally.
    materialize_delay_mock = MagicMock()
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        materialize_delay_mock,
    )
    # ``enqueue_notify_user`` does a real SADD against
    # ``schedule:materialize_notify_pending``. Real Redis handles this
    # natively, so no patching is needed — tests that want to observe
    # the queued users just call ``redis_client.smembers(...)``.

    return {
        "add_order": add_mock,
        "remove_order": remove_mock,
        "pin_order": pin_mock,
        "unpin_order": unpin_mock,
        "apply_schedule": apply_mock,
        "broadcast": broadcast_mock,
        "notify_user": notify_mock,
        "delay": delay_mock,
        "materialize_delay": materialize_delay_mock,
    }


# ---------------------------------------------------------------------------
# run_scheduling_task — happy path
# ---------------------------------------------------------------------------


def test_run_scheduling_processes_two_adds(
    monkeypatch: pytest.MonkeyPatch, redis_client: Redis
) -> None:
    op1 = _make_op(order_number="ORD-001")
    op2 = _make_op(order_number="ORD-002", wafer_quantity=2000)
    # ZADD with monotonic seq: op1 has smaller score ⇒ ZPOPMIN'd first.
    _enqueue(redis_client, op1)
    _enqueue(redis_client, op2)

    mocks = _patch_common(monkeypatch)

    result = run_scheduling_task.apply()

    assert result.successful(), result.traceback
    # Per-compound design: each invocation handles one compound and
    # re-triggers itself. The test's auto-retrigger delay-side-effect
    # bridges those calls so the whole queue drains under a single
    # test-driven apply().
    assert mocks["add_order"].call_count == 2
    # Phase 4 fast/slow split: fast path no longer calls apply_schedule
    # or broadcast. Both moved to the deferred materializer.
    assert mocks["apply_schedule"].call_count == 0
    assert mocks["broadcast"].call_count == 0
    # Per-compound: notify_user(compound_accepted) and
    # materialize_schedule_task.delay() fire on each success.
    assert mocks["notify_user"].call_count == 2
    for call in mocks["notify_user"].call_args_list:
        assert call.kwargs["message"]["type"] == "schedule.compound_accepted"
    assert mocks["materialize_delay"].call_count == 2
    # run_scheduling_task.delay() fired once between compound1 and
    # compound2 (after compound1 sees the second still queued).
    assert mocks["delay"].call_count == 1
    # Final status: idle, with a finished_at timestamp
    status_doc = json.loads(redis_client.get(STATUS_KEY))
    assert status_doc["state"] == "idle"
    assert status_doc["finished_at"] is not None
    # State persisted by the fast path (cheap O(n) serialize).
    assert redis_client.get(STATE_KEY) is not None
    # Both requesters got SADD'd into the materializer's notify queue.
    assert redis_client.scard("schedule:materialize_notify_pending") == 2


# ---------------------------------------------------------------------------
# run_scheduling_task — capacity exceeded notifies the requester
# ---------------------------------------------------------------------------


def test_run_scheduling_notifies_user_on_capacity_exceeded(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Compound containing a failing ``add`` rolls back + WS-notifies.

    Compound model contract: an op-level failure inside a compound triggers
    a snapshot rollback and ``schedule.compound_failed`` to the compound's
    ``requested_by``. The successful op inside a separate compound runs
    normally on its own turn.
    """

    failing_id = uuid.uuid4()
    failing_user = uuid.uuid4()
    failing_compound_id = uuid.uuid4()
    compound_fail = _make_op(
        order_id=failing_id,
        order_number="ORD-FAIL",
        wafer_quantity=50_000,
        requested_by=failing_user,
    )
    compound_fail["compound_id"] = str(failing_compound_id)
    compound_ok = _make_op(order_number="ORD-OK", wafer_quantity=1000)
    _enqueue(redis_client, compound_fail)
    _enqueue(redis_client, compound_ok)

    add_mock = MagicMock(
        side_effect=[
            ScheduleResult(
                status="capacity_exceeded",
                order_id=failing_id,
                message="too big",
            ),
            ScheduleResult(status="success"),
        ]
    )
    mocks = _patch_common(monkeypatch, add_order=add_mock)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Both compounds were popped; failed compound rolled back, successful
    # compound finalized normally.
    assert mocks["add_order"].call_count == 2

    # Phase 4 fast/slow split: notify_user fires twice now —
    #   1) compound_failed for the rolled-back compound
    #   2) compound_accepted for the successful compound
    # (broadcast / apply_schedule no longer fire here; they happen in
    # the deferred materializer.)
    notify_calls = mocks["notify_user"].call_args_list
    by_type = {c.kwargs["message"]["type"]: c.kwargs for c in notify_calls}
    assert set(by_type.keys()) == {
        "schedule.compound_failed",
        "schedule.compound_accepted",
    }
    failed_msg = by_type["schedule.compound_failed"]["message"]
    assert by_type["schedule.compound_failed"]["user_id"] == failing_user
    assert failed_msg["compound_id"] == str(failing_compound_id)
    assert failed_msg["failed_op"] == "add"
    assert failed_msg["failed_op_index"] == 0
    assert failed_msg["order_id"] == str(failing_id)
    assert failed_msg["reason"] == "capacity_exceeded"
    assert failed_msg["rolled_back"] is True

    # Only the successful compound dispatches the materializer.
    assert mocks["materialize_delay"].call_count == 1


def test_run_scheduling_notifies_user_on_remove_failure(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Compound containing a failing ``remove`` rolls back + WS-notifies.

    Realistic trigger: a stale producer pushed a ``remove`` for an order
    that's no longer in the pq (e.g. it was pinned out, or already removed
    by a previous compound). The compound rolls back (no-op since remove
    was the only op) and the requester gets ``schedule.compound_failed``
    with ``failed_op="remove"`` so the UI can surface the inconsistency.
    """

    failing_id = uuid.uuid4()
    failing_user = uuid.uuid4()
    failing_compound_id = uuid.uuid4()
    compound = _make_op(
        op="remove",
        order_id=failing_id,
        order_number="ORD-DEL",
        requested_by=failing_user,
    )
    compound["compound_id"] = str(failing_compound_id)
    _enqueue(redis_client, compound)

    remove_mock = MagicMock(
        return_value=ScheduleResult(
            status="deadline_too_far",
            order_id=failing_id,
            message="Deadline outside the 30-day scheduling horizon.",
        )
    )
    mocks = _patch_common(monkeypatch)
    monkeypatch.setattr("app.workers.scheduling.remove_order", remove_mock)
    mocks["remove_order"] = remove_mock

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    assert remove_mock.call_count == 1
    assert mocks["notify_user"].call_count == 1
    kwargs = mocks["notify_user"].call_args.kwargs
    assert kwargs["user_id"] == failing_user
    msg = kwargs["message"]
    assert msg["type"] == "schedule.compound_failed"
    assert msg["compound_id"] == str(failing_compound_id)
    assert msg["failed_op"] == "remove"
    assert msg["order_id"] == str(failing_id)
    assert msg["reason"] == "deadline_too_far"
    assert msg["rolled_back"] is True


# ---------------------------------------------------------------------------
# run_scheduling_task — re-trigger when ops arrive mid-flight
# ---------------------------------------------------------------------------


def test_run_scheduling_writes_status_failed_on_exception_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """When ``run_scheduling_task`` body raises, it MUST:

    1. Write ``schedule:status`` to ``failed`` with the error string captured
       (so ``GET /schedule/status`` exposes the breakage to operators).
    2. NOT leave status stuck at ``running`` — that would 409 every future
       ``POST /schedule/trigger`` permanently and the only escape is
       hand-editing Redis.
    3. Re-raise so Celery records the traceback in its result backend.

    Pre-fix the body had no ``except``, so any exception left status frozen
    at ``running`` and silently broke /trigger. Locking this contract with a
    test means a future refactor can't strip the except block without
    flipping a red light.
    """
    _enqueue(redis_client, _make_op(order_number="ORD-BOOM"))

    # add_order is the realistic crash point — bug in segment tree code
    # would manifest there. Any internal raise has the same contract.
    failing_add = MagicMock(side_effect=RuntimeError("segment tree corrupted"))
    mocks = _patch_common(monkeypatch, add_order=failing_add)

    result = run_scheduling_task.apply()
    # Celery sees the failure (traceback is in result.traceback).
    assert not result.successful()
    assert "segment tree corrupted" in (result.traceback or "")

    # Status doc shows the failure so operators see it via /schedule/status.
    raw = redis_client.get(STATUS_KEY)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["state"] == "failed"
    assert payload["error"] == "segment tree corrupted"
    assert payload["finished_at"] is not None
    # Crucially: NOT stuck at running (would hard-block /trigger).
    assert payload["state"] != "running"

    # No re-trigger fired on the failure path — there's nothing to gain
    # from looping a known-broken task.
    assert not mocks["delay"].called


def test_run_scheduling_retriggers_when_more_ops_arrive(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """A compound arriving mid-processing must be picked up via re-trigger
    instead of waiting for an external dispatch.

    Phase 4 changed the fast path so ``compute_schedule`` is no longer
    called in this task body — the mid-task injection point moves to
    ``add_order`` (which IS called during compound processing). The
    re-trigger check still happens at the end via ``zcard``.
    """
    _enqueue(redis_client, _make_op())

    injected = {"done": False}

    def add_with_late_injection(_state: SchedulerState, _order: SchedulingOrder) -> ScheduleResult:
        if not injected["done"]:
            _enqueue(redis_client, _make_op(order_number="LATE"))
            injected["done"] = True
        return ScheduleResult(status="success")

    mocks = _patch_common(monkeypatch, add_order=MagicMock(side_effect=add_with_late_injection))

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # First compound processed, then re-triggered to pick up LATE.
    assert mocks["delay"].called


def test_run_scheduling_processes_shrink_group_before_grow(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Compound updates' ops must respect their group: shrink-group runs to
    completion before grow-group, regardless of the queue's RPOP order."""

    # Producer pushed in order: a defer (shrink remove + shrink add), then an
    # advance (grow remove + grow add). Worker should process all four shrink
    # ops first (in original order), then all four grow ops.
    defer_remove = _make_op(op="remove", group="shrink", order_number="DEFER-R")
    defer_add = _make_op(op="add", group="shrink", order_number="DEFER-A")
    advance_remove = _make_op(op="remove", group="grow", order_number="ADVANCE-R")
    advance_add = _make_op(op="add", group="grow", order_number="ADVANCE-A")
    for op in (defer_remove, defer_add, advance_remove, advance_add):
        _enqueue(redis_client, op)

    call_order: list[str] = []

    def track_add(_state: SchedulerState, order: SchedulingOrder) -> ScheduleResult:
        call_order.append(f"add:{order.order_number}")
        return ScheduleResult(status="success")

    def track_remove(_state: SchedulerState, order: SchedulingOrder) -> ScheduleResult:
        call_order.append(f"remove:{order.order_number}")
        return ScheduleResult(status="success")

    monkeypatch.setattr("app.workers.scheduling.add_order", MagicMock(side_effect=track_add))
    monkeypatch.setattr("app.workers.scheduling.remove_order", MagicMock(side_effect=track_remove))
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _s: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    # Silence the Phase-4 slow-path side effects so CI without a real Redis
    # doesn't hit ``schedule_queue._redis().sadd`` / Celery .delay.
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )
    _install_auto_retrigger_delay(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # All shrink-group ops fire before any grow-group op; FIFO inside each group.
    assert call_order == [
        "remove:DEFER-R",
        "add:DEFER-A",
        "remove:ADVANCE-R",
        "add:ADVANCE-A",
    ]


def test_run_scheduling_lets_late_shrink_jump_pending_grow(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """A shrink op LPUSH'd while a grow batch is being processed must be
    picked up *before* the remaining grow ops, not after them.

    Setup: queue starts with two grow ops [GROW-1 (older), GROW-2]. A side
    effect on the *first* grow's add_order injects a fresh shrink op into
    the queue. The next pop must therefore see the new shrink and run it
    before GROW-2."""
    _enqueue(redis_client, _make_op(op="add", group="grow", order_number="GROW-1"))
    _enqueue(redis_client, _make_op(op="add", group="grow", order_number="GROW-2"))

    call_order: list[str] = []

    def track_remove(_state: SchedulerState, order: SchedulingOrder) -> ScheduleResult:
        call_order.append(f"remove:{order.order_number}")
        return ScheduleResult(status="success")

    def track_add(_state: SchedulerState, order: SchedulingOrder) -> ScheduleResult:
        call_order.append(f"add:{order.order_number}")
        # Mid-task arrival: producer LPUSHes a new shrink right after the
        # first grow finishes processing. The next pop should pick it up.
        if order.order_number == "GROW-1":
            _enqueue(
                redis_client,
                _make_op(op="remove", group="shrink", order_number="LATE-SHRINK"),
            )
        return ScheduleResult(status="success")

    monkeypatch.setattr("app.workers.scheduling.add_order", MagicMock(side_effect=track_add))
    monkeypatch.setattr("app.workers.scheduling.remove_order", MagicMock(side_effect=track_remove))
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _s: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    # Silence the Phase-4 slow-path side effects so CI without a real Redis
    # doesn't hit ``schedule_queue._redis().sadd`` / Celery .delay.
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )
    _install_auto_retrigger_delay(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Order must be: GROW-1 (only op at start), LATE-SHRINK (jumped ahead
    # because it's a shrink), GROW-2 (last remaining grow).
    assert call_order == [
        "add:GROW-1",
        "remove:LATE-SHRINK",
        "add:GROW-2",
    ]


def test_run_scheduling_skips_retrigger_when_queue_drained(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    # No pending ops at all.
    mocks = _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    assert mocks["add_order"].call_count == 0
    assert not mocks["delay"].called


def test_run_scheduling_yields_retrigger_to_waiter(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """If the waiter flag is set, ``run_scheduling_task`` MUST NOT
    re-trigger itself even when ops remain — the waiter
    (advance_day / rebuild) is in ``_wait_for_idle_run`` right now and will
    fire the next ``run_scheduling_task.delay()`` after its own work.

    Without this yield, the waiter and the re-triggered run_task race on
    ``schedule:state``: the waiter sees status flip to idle, breaks out of
    its wait loop, but run_task hasn't yet fired the re-trigger; run_task
    fires the re-trigger a few microseconds later, both end up running
    concurrently.
    """
    # Pre-set the waiter flag: a waiter is waiting on us right now.
    redis_client.set("schedule:waiter_pending", "1", ex=600)
    # One op processed + one still queued = zcard > 0 at end of task →
    # would normally fire delay() if not for the yield.
    _enqueue(redis_client, _make_op(order_number="ORD-A"))
    _enqueue(redis_client, _make_op(order_number="ORD-B"))

    # Plain delay (no auto-retrigger) — we want to verify it's NOT called.
    add_mock = MagicMock(return_value=ScheduleResult(status="success"))
    monkeypatch.setattr("app.workers.scheduling.add_order", add_mock)
    monkeypatch.setattr(
        "app.workers.scheduling.remove_order",
        MagicMock(return_value=ScheduleResult(status="success")),
    )
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _s: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    # Silence the Phase-4 slow-path side effects so CI without a real Redis
    # doesn't hit ``schedule_queue._redis().sadd`` / Celery .delay.
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )
    plain_delay = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", plain_delay)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # First op was processed (per-op design)…
    assert add_mock.call_count == 1
    # …second op still pending (zcard > 0)…
    assert redis_client.zcard(PENDING_OPS_KEY) == 1
    # …but delay was NOT fired because the waiter holds responsibility for
    # the next re-trigger.
    assert plain_delay.call_count == 0


# ---------------------------------------------------------------------------
# Waiter flag — advance_day / rebuild set it, finally clears it
# ---------------------------------------------------------------------------


def test_advance_day_sets_waiter_flag_then_clears_it(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """advance_day_task must hold the waiter flag for the duration of its
    body so a concurrent ``run_scheduling_task`` finishing during the wait
    yields its re-trigger to us. Cleared in ``finally`` so a clean run
    leaves the flag unset for future re-triggers."""
    monkeypatch.setattr(
        "app.workers.scheduling._get_status",
        lambda: {"state": "idle"},
    )
    _patch_rebuild_time(monkeypatch)  # also covers advance_day's time module

    initial = SchedulerState.initial(date(2026, 5, 5))
    advanced = SchedulerState.initial(date(2026, 5, 6))
    monkeypatch.setattr("app.workers.scheduling._load_state", lambda: initial)

    # Capture the flag's value at the moment advance_day is called — this
    # is "during the body" so the flag should be set.
    flag_during: list[str | None] = []

    def observe_advance(_state: SchedulerState) -> SchedulerState:
        flag_during.append(redis_client.get("schedule:waiter_pending"))
        return advanced

    monkeypatch.setattr("app.workers.scheduling.advance_day", observe_advance)
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", MagicMock(return_value=[]))
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    result = advance_day_task.apply()
    assert result.successful(), result.traceback

    # Flag was set while the body was running.
    assert flag_during == ["1"]
    # Flag was cleared after the task finished.
    assert redis_client.get("schedule:waiter_pending") is None


def test_advance_day_clears_waiter_flag_even_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """If the waiter body crashes mid-flight the flag MUST still be cleared,
    otherwise future ``run_scheduling_task`` invocations would yield to a
    phantom waiter forever (until TTL expires, which is too long to wait).

    Guarded by the ``finally`` clause around the body.
    """
    monkeypatch.setattr("app.workers.scheduling._get_status", lambda: {"state": "idle"})
    _patch_rebuild_time(monkeypatch)

    monkeypatch.setattr(
        "app.workers.scheduling._load_state",
        lambda: SchedulerState.initial(date(2026, 5, 5)),
    )
    # advance_day raises mid-body.
    monkeypatch.setattr(
        "app.workers.scheduling.advance_day",
        MagicMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    # apply() returns the failed result rather than raising up to the test.
    result = advance_day_task.apply()
    assert not result.successful()
    # Flag is cleared regardless of the exception.
    assert redis_client.get("schedule:waiter_pending") is None


def test_rebuild_clears_waiter_flag_even_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Mirror test for ``rebuild_schedule_task``: if any step raises (e.g.
    DB layer down, list_for_scheduler errors), the waiter flag still gets
    cleared so the system recovers without waiting for TTL expiry."""
    monkeypatch.setattr("app.workers.scheduling._get_status", lambda: {"state": "idle"})
    _patch_rebuild_time(monkeypatch)

    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    # list_for_scheduler raises (e.g. DB connection blew up).
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.list_for_scheduler",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    result = rebuild_schedule_task.apply()
    assert not result.successful()
    assert redis_client.get("schedule:waiter_pending") is None


# ---------------------------------------------------------------------------
# Status-claim — advance_day / rebuild own schedule:status while working
# ---------------------------------------------------------------------------


def test_advance_day_claims_status_running_during_body_and_clears_to_idle(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Status-claim race fix: between ``_wait_for_idle_run`` returning and
    the body finishing, ``schedule:status`` MUST read ``running`` so a
    concurrent ``POST /schedule/trigger`` returns 409 instead of dispatching
    a second run that races state writes. Inner finally must restore
    ``idle``.

    Without the fix, the window read ``idle`` and any /trigger or
    /operations call landed a parallel ``run_scheduling_task`` that wrote
    over the waiter's state.
    """
    monkeypatch.setattr(
        "app.workers.scheduling._get_status",
        lambda: {"state": "idle"},
    )
    _patch_rebuild_time(monkeypatch)

    initial = SchedulerState.initial(date(2026, 5, 5))
    advanced = SchedulerState.initial(date(2026, 5, 6))
    monkeypatch.setattr("app.workers.scheduling._load_state", lambda: initial)

    # Capture status from real Redis at the moment ``advance_day`` runs —
    # this is mid-body, so it must read "running" (not "idle").
    status_during_body: list[str] = []

    def observe_advance(_state: SchedulerState) -> SchedulerState:
        raw = redis_client.get("schedule:status")
        assert raw is not None
        status_during_body.append(json.loads(raw)["state"])
        return advanced

    monkeypatch.setattr("app.workers.scheduling.advance_day", observe_advance)
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", MagicMock(return_value=[]))
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    result = advance_day_task.apply()
    assert result.successful(), result.traceback

    # Mid-body: status was "running".
    assert status_during_body == ["running"]
    # Post-body: inner finally restored "idle".
    raw = redis_client.get("schedule:status")
    assert raw is not None
    final = json.loads(raw)
    assert final["state"] == "idle"
    assert final["finished_at"] is not None


def test_advance_day_writes_status_failed_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """If the body raises after status was claimed, status MUST flip to
    ``failed`` (with the error captured) — NOT ``idle``. Writing ``idle``
    after a real failure makes ``GET /schedule/status`` show a healthy
    scheduler and silently masks the broken run from operators.

    Also asserts status doesn't stick at ``running`` (that would 409 every
    future ``/trigger``). The acceptable terminal states on exception are
    ``failed`` (visible to ops) — never ``running`` and never ``idle``.
    """
    monkeypatch.setattr("app.workers.scheduling._get_status", lambda: {"state": "idle"})
    _patch_rebuild_time(monkeypatch)

    monkeypatch.setattr(
        "app.workers.scheduling._load_state",
        lambda: SchedulerState.initial(date(2026, 5, 5)),
    )
    monkeypatch.setattr(
        "app.workers.scheduling.advance_day",
        MagicMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    result = advance_day_task.apply()
    assert not result.successful()

    raw = redis_client.get("schedule:status")
    assert raw is not None
    payload = json.loads(raw)
    assert payload["state"] == "failed"
    # Error string carried through so operators reading /schedule/status see
    # what broke without having to grep Celery logs.
    assert payload["error"] == "boom"
    assert payload["finished_at"] is not None


# ---------------------------------------------------------------------------
# advance_day_task — waits for in-flight run, then advances and re-fires
# ---------------------------------------------------------------------------


def test_advance_day_waits_then_advances_finalizes_and_retriggers(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """``advance_day_task`` must wait for any in-flight run, advance the
    horizon, finalize (compute → apply → save → broadcast) directly, and
    only then re-trigger ``run_scheduling_task`` if pending ops remain.

    Pre-seed one pending op so we can verify the conditional retrigger; the
    no-pending-ops branch is implicitly covered by checking call count
    against the ``zcard`` decision.
    """

    # One pending op so the conditional retrigger fires.
    _enqueue(redis_client, _make_op(order_number="POST-ADVANCE"))

    # First poll: still running. Second poll: idle ⇒ break.
    states = iter(
        [
            {"state": "running"},
            {"state": "idle"},
        ]
    )
    monkeypatch.setattr(
        "app.workers.scheduling._get_status",
        lambda: next(states),
    )

    sleep_calls: list[float] = []

    # Advance monotonic by a tiny step on every call so the timeout
    # guard inside advance_day_task is well-defined.
    monotonic_value = [0.0]

    def fake_monotonic() -> float:
        monotonic_value[0] += 0.1
        return monotonic_value[0]

    fake_time = types.SimpleNamespace(
        sleep=lambda secs: sleep_calls.append(secs),
        monotonic=fake_monotonic,
    )
    monkeypatch.setattr("app.workers.scheduling.time", fake_time)

    initial = SchedulerState.initial(date(2026, 5, 5))
    advanced = SchedulerState.initial(date(2026, 5, 6))

    monkeypatch.setattr("app.workers.scheduling._load_state", lambda: initial)

    saved: list[SchedulerState] = []
    monkeypatch.setattr(
        "app.workers.scheduling._save_state",
        lambda s: saved.append(s),
    )

    advance_mock = MagicMock(return_value=advanced)
    monkeypatch.setattr("app.workers.scheduling.advance_day", advance_mock)

    # Stubs for the finalize chain (compute → apply → save → broadcast).
    # Phase 3: compute_schedule is called TWICE per advance_day_task
    # invocation — once on the OLD state to identify today's-locked-in
    # orders, once on the NEW state for apply_schedule. Return [] for both
    # to keep this happy-path lightweight.
    compute_mock = MagicMock(return_value=[])
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", compute_mock)
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    apply_mock = MagicMock(return_value=0)
    monkeypatch.setattr("app.workers.scheduling.order_service.apply_schedule", apply_mock)
    # Phase 3: status-transition repo calls.
    mark_completed_mock = MagicMock(return_value=0)
    monkeypatch.setattr(
        "app.workers.scheduling.order_repo.mark_completed_outside_set",
        mark_completed_mock,
    )
    mark_in_prod_mock = MagicMock(return_value=0)
    monkeypatch.setattr(
        "app.workers.scheduling.order_repo.mark_in_production",
        mark_in_prod_mock,
    )
    broadcast_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", broadcast_mock)

    # Plain delay mock — we only want to verify advance_day_task fires it
    # once at the end (not the auto-retrigger flow that's exercised by
    # run_scheduling_task tests).
    delay_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", delay_mock)

    result = advance_day_task.apply()
    assert result.successful(), result.traceback

    # Slept exactly once (the running-state iteration before idle).
    assert len(sleep_calls) == 1
    # advance_day called with the loaded state.
    advance_mock.assert_called_once_with(initial)
    # compute_schedule fires twice (old state + new state). Last call was
    # with the advanced state so apply_schedule had the post-shift view.
    assert compute_mock.call_count == 2
    assert compute_mock.call_args_list[-1].args == (advanced,)
    apply_mock.assert_called_once()
    # Status workflow ran: complete-stale then mark-today (Phase 3).
    mark_completed_mock.assert_called_once()
    mark_in_prod_mock.assert_called_once()
    broadcast_mock.assert_called_once_with({"type": "schedule.updated"})
    # The advanced state was persisted.
    assert saved == [advanced]
    # A scheduling re-run was kicked off because there was 1 pending op.
    assert delay_mock.called


def test_advance_day_marks_today_orders_in_production(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Phase 3 case 17: advance_day flips ``today's d0`` orders to
    ``in_production`` and ``previously-in_production-now-out-of-state``
    orders to ``completed``.

    Setup: OLD state's compute_schedule returns one ScheduledResult on
    today's date (``order_X``); new state's pq contains a different
    ``order_Y`` (still being scheduled for the future). Assert the
    mark_in_production call gets ``{order_X}`` and mark_completed_outside_set
    gets ``{order_Y}`` (the only alive id).
    """
    monkeypatch.setattr(
        "app.workers.scheduling._get_status",
        lambda: {"state": "idle"},
    )
    _patch_rebuild_time(monkeypatch)

    today = date(2026, 5, 5)
    tomorrow = today + timedelta(days=1)
    order_x = uuid.uuid4()  # today's d0 production
    order_y = uuid.uuid4()  # still scheduled in new state

    old_state = SchedulerState.initial(today)
    new_state = SchedulerState.initial(tomorrow)
    order_y_obj = SchedulingOrder(
        order_id=order_y,
        order_number="ORD-Y",
        wafer_quantity=1000,
        deadline=tomorrow + timedelta(days=2),
    )
    # SortedKeyList uses ``add`` (not append) to maintain sort order; mirror
    # the index dict so contains-check / lookup paths still work.
    new_state.priority_queue.add(order_y_obj)
    new_state.pq_index[order_y] = order_y_obj

    monkeypatch.setattr("app.workers.scheduling._load_state", lambda: old_state)
    monkeypatch.setattr(
        "app.workers.scheduling.advance_day",
        MagicMock(return_value=new_state),
    )

    # compute_schedule called twice:
    #   1) on old_state for today_locked_in detection → ScheduledResult on today.
    #   2) on new_state for apply_schedule → ScheduledResult on tomorrow.
    compute_side_effects = iter(
        [
            [ScheduledResult(order_id=order_x, scheduled_date=today, quantity=1000)],
            [ScheduledResult(order_id=order_y, scheduled_date=tomorrow, quantity=1000)],
        ]
    )
    monkeypatch.setattr(
        "app.workers.scheduling.compute_schedule",
        lambda _state: next(compute_side_effects),
    )

    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )

    mark_completed_mock = MagicMock(return_value=0)
    monkeypatch.setattr(
        "app.workers.scheduling.order_repo.mark_completed_outside_set",
        mark_completed_mock,
    )
    mark_in_prod_mock = MagicMock(return_value=1)
    monkeypatch.setattr(
        "app.workers.scheduling.order_repo.mark_in_production",
        mark_in_prod_mock,
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    result = advance_day_task.apply()
    assert result.successful(), result.traceback

    # in_production set = today's d0 order = {order_x}.
    in_prod_args = mark_in_prod_mock.call_args.args
    assert in_prod_args[1] == {order_x}

    # completed-outside-set called with new state's alive ids = {order_y}.
    completed_args = mark_completed_mock.call_args.args
    assert completed_args[1] == {order_y}


# ---------------------------------------------------------------------------
# rebuild_schedule_task — waits for in-flight run, rebuilds, notifies, retriggers
# ---------------------------------------------------------------------------


def _patch_rebuild_time(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Stub ``time.sleep``/``time.monotonic`` inside the worker module.

    Returns the list that ``sleep`` calls append to so tests can assert
    polling cadence. Monotonic advances by 0.1s on every read so the
    rebuild's 5-minute deadline never trips inside the test.
    """
    sleep_calls: list[float] = []
    monotonic_value = [0.0]

    def fake_monotonic() -> float:
        monotonic_value[0] += 0.1
        return monotonic_value[0]

    fake_time = types.SimpleNamespace(
        sleep=lambda secs: sleep_calls.append(secs),
        monotonic=fake_monotonic,
    )
    monkeypatch.setattr("app.workers.scheduling.time", fake_time)
    return sleep_calls


def test_rebuild_task_waits_for_running_then_rebuilds_and_retriggers(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Full happy path: status flips from running→idle while rebuild_task is
    polling, then it rebuilds, persists, and re-triggers run_scheduling_task.

    No skipped orders in this path — the next test covers that branch.
    """

    base = date(2026, 5, 5)
    redis_client.set(STATE_KEY, SchedulerState.initial(base).to_json())
    # One pending op so the conditional retrigger fires after rebuild.
    _enqueue(redis_client, _make_op(order_number="POST-REBUILD"))

    statuses = iter([{"state": "running"}, {"state": "idle"}])
    monkeypatch.setattr(
        "app.workers.scheduling._get_status",
        lambda: next(statuses),
    )

    sleep_calls = _patch_rebuild_time(monkeypatch)

    # DB layer: list_for_scheduler returns one order with a known creator.
    order_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    pulled_order = SchedulingOrder(
        order_id=order_id,
        order_number="ORD-OK",
        wafer_quantity=100,
        deadline=base + timedelta(days=3),
    )
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.list_for_scheduler",
        lambda db: ([pulled_order], {order_id: creator_id}),
    )

    # rebuild_state succeeds with no skipped.
    rebuilt_state = SchedulerState.initial(base)
    rebuilt_state.priority_queue.add(pulled_order)
    rebuilt_state.pq_index[pulled_order.order_id] = pulled_order
    rebuild_mock = MagicMock(return_value=(rebuilt_state, []))
    monkeypatch.setattr("app.workers.scheduling.rebuild_state", rebuild_mock)

    # Stubs for the finalize chain that rebuild_schedule_task now invokes
    # directly (rather than relying on run_scheduling_task).
    compute_mock = MagicMock(return_value=[])
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", compute_mock)
    apply_mock = MagicMock(return_value=0)
    monkeypatch.setattr("app.workers.scheduling.order_service.apply_schedule", apply_mock)
    broadcast_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", broadcast_mock)

    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)

    delay_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", delay_mock)

    result = rebuild_schedule_task.apply()
    assert result.successful(), result.traceback

    # Slept exactly once before status flipped to idle.
    assert len(sleep_calls) == 1
    # rebuild_state called with the orders pulled from DB.
    rebuild_mock.assert_called_once_with([pulled_order], base)
    # _finalize_run did its full pipeline on the rebuilt state.
    compute_mock.assert_called_once_with(rebuilt_state)
    apply_mock.assert_called_once()
    broadcast_mock.assert_called_once_with({"type": "schedule.updated"})
    # New state was persisted (via _save_state inside _finalize_run).
    saved_raw = redis_client.get(STATE_KEY)
    assert saved_raw is not None
    assert saved_raw == rebuilt_state.to_json()
    # No skipped → no notify_user calls.
    notify_mock.assert_not_called()
    # run_scheduling_task was kicked off because POST-REBUILD is pending.
    assert delay_mock.called


def test_rebuild_task_notifies_each_skipped_orders_creator(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """When ``rebuild_state`` returns a non-empty ``skipped`` list, the task
    must push a ``schedule.rebuild_skipped`` WebSocket message to each
    skipped order's creator (looked up via the ``creators`` map). Skipped
    orders without a known creator are silently dropped (defensive)."""

    base = date(2026, 5, 5)
    redis_client.set(STATE_KEY, SchedulerState.initial(base).to_json())

    monkeypatch.setattr(
        "app.workers.scheduling._get_status",
        lambda: {"state": "idle"},
    )
    _patch_rebuild_time(monkeypatch)

    skip_a_id = uuid.uuid4()
    skip_b_id = uuid.uuid4()
    creator_a = uuid.uuid4()
    creator_b = uuid.uuid4()

    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.list_for_scheduler",
        lambda db: ([], {skip_a_id: creator_a, skip_b_id: creator_b}),
    )

    skipped = [
        SkippedOrder(order_id=skip_a_id, order_number="ORD-A", reason="deadline_too_far"),
        SkippedOrder(order_id=skip_b_id, order_number="ORD-B", reason="deadline_too_far"),
    ]
    monkeypatch.setattr(
        "app.workers.scheduling.rebuild_state",
        lambda orders, base_date: (SchedulerState.initial(base_date), skipped),
    )

    # _finalize_run pipeline stubs.
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", MagicMock(return_value=[]))
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule", MagicMock(return_value=0)
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())

    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    result = rebuild_schedule_task.apply()
    assert result.successful(), result.traceback

    # Each skipped order's creator was notified exactly once with the
    # schedule.rebuild_skipped envelope.
    assert notify_mock.call_count == 2
    seen_user_ids = {call.kwargs["user_id"] for call in notify_mock.call_args_list}
    assert seen_user_ids == {creator_a, creator_b}
    for call in notify_mock.call_args_list:
        msg = call.kwargs["message"]
        assert msg["type"] == "schedule.rebuild_skipped"
        assert msg["reason"] == "deadline_too_far"
        assert msg["order_number"] in {"ORD-A", "ORD-B"}


def test_rebuild_task_uses_today_when_no_existing_state(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """If Redis has no ``schedule:state`` (first deploy / wiped), rebuild
    falls back to ``datetime.now().date()`` as base_date."""
    # Note: do NOT pre-seed STATE_KEY.
    monkeypatch.setattr(
        "app.workers.scheduling._get_status",
        lambda: {"state": "idle"},
    )
    _patch_rebuild_time(monkeypatch)

    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.list_for_scheduler",
        lambda db: ([], {}),
    )

    captured: list[date] = []

    def capture_base(
        orders: list[Any], base_date: date
    ) -> tuple[SchedulerState, list[SkippedOrder]]:
        captured.append(base_date)
        return (SchedulerState.initial(base_date), [])

    monkeypatch.setattr("app.workers.scheduling.rebuild_state", capture_base)
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", MagicMock(return_value=[]))
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule", MagicMock(return_value=0)
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", MagicMock())
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    result = rebuild_schedule_task.apply()
    assert result.successful(), result.traceback

    # Falls back to today (UTC).
    from datetime import UTC
    from datetime import datetime as _dt

    assert len(captured) == 1
    assert captured[0] == _dt.now(tz=UTC).date()


# ---------------------------------------------------------------------------
# Pin / Unpin op dispatch
# ---------------------------------------------------------------------------


def test_run_scheduling_dispatches_pin_op_with_fake_deadline(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """A queued ``op="pin"`` must reach ``pin_order(state, order, fake_deadline)``
    with the fake_deadline parsed back into a ``date``. This is the contract
    that lets the API encode pin requests as a normal ScheduleOperationRequest.
    """
    op = _make_op(
        op="pin",
        group="grow",
        order_number="PIN-ME",
        deadline="2026-05-15",
        fake_deadline="2026-05-12",
    )
    _enqueue(redis_client, op)

    mocks = _patch_common(monkeypatch)
    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    assert mocks["pin_order"].call_count == 1
    args, _ = mocks["pin_order"].call_args
    # signature: pin_order(state, order, fake_deadline)
    _, order_arg, fake_arg = args
    assert order_arg.order_number == "PIN-ME"
    assert fake_arg == date(2026, 5, 12)


def test_run_scheduling_rollback_restores_state_on_mid_compound_failure(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Saga rollback invariant: a compound's earlier successful ops are
    undone when a later op fails.

    Setup: a compound of [remove, add] where ``add`` fails. State going in
    has the order in pq with old qty; after rollback the order MUST still
    be in pq with old qty — the remove that succeeded mid-compound got
    reversed by snapshot restore.

    This locks the Phase-2 atomicity contract: no partial mutation is ever
    observable to ``_finalize_run`` after a failure.
    """
    from datetime import date as _date

    from app.services.scheduling import SchedulingOrder, add_order

    # Pre-seed state in Redis with one order so remove has something real
    # to undo and add can credibly fail.
    state = SchedulerState.initial(_date(2026, 5, 5))
    order_id = uuid.uuid4()
    add_order(
        state,
        SchedulingOrder(
            order_id=order_id,
            order_number="ORD-EXISTING",
            wafer_quantity=1000,
            deadline=_date(2026, 5, 10),
        ),
    )
    redis_client.set(STATE_KEY, state.to_json())

    failing_user = uuid.uuid4()
    failing_compound_id = uuid.uuid4()
    compound = _make_compound(
        ops=[
            _make_leaf_op(
                op="remove",
                order_id=order_id,
                order_number="ORD-EXISTING",
                wafer_quantity=1000,
                deadline="2026-05-10",
            ),
            _make_leaf_op(
                op="add",
                order_id=order_id,
                order_number="ORD-EXISTING",
                wafer_quantity=999_999,  # too large — will fail capacity
                deadline="2026-05-10",
            ),
        ],
        group="grow",
        requested_by=failing_user,
        compound_id=failing_compound_id,
    )
    _enqueue(redis_client, compound)

    # No mocks for add/remove — we want the REAL algorithm to fail on the
    # huge qty, so we can observe true state rollback.
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Compound failed → state in Redis should be UNCHANGED from pre-compound.
    saved_raw = redis_client.get(STATE_KEY)
    assert saved_raw is not None
    # State should NOT have been mutated by the failed compound. The
    # cleanest check: the pre-compound snapshot we put in equals what's
    # in Redis now (i.e., _save_state was never called because finalize
    # only runs on success).
    saved = SchedulerState.from_json(saved_raw)
    assert len(saved.priority_queue) == 1
    assert saved.priority_queue[0].order_id == order_id
    assert saved.priority_queue[0].wafer_quantity == 1000

    # WS notify shows the rollback to the requester.
    assert notify_mock.call_count == 1
    msg = notify_mock.call_args.kwargs["message"]
    assert msg["type"] == "schedule.compound_failed"
    assert msg["failed_op"] == "add"
    assert msg["failed_op_index"] == 1
    assert msg["rolled_back"] is True


def test_run_scheduling_rejects_compound_with_op_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Worker-side tamper guard: a compound whose declared ``op_count``
    doesn't match ``len(ops)`` is rejected before any leaf op runs.

    Schema validation at enqueue time enforces this, but the Redis
    sorted-set member could in principle be corrupted post-enqueue (manual
    redis-cli surgery, mid-byte truncation, etc.). The worker re-checks
    and fails the whole compound rather than execute a half-truncated
    business action.
    """
    failing_user = uuid.uuid4()
    failing_compound_id = uuid.uuid4()
    compound = _make_compound(
        ops=[
            _make_leaf_op(order_number="ORD-T"),
        ],
        requested_by=failing_user,
        compound_id=failing_compound_id,
        op_count=99,  # lies — only 1 op
    )
    _enqueue(redis_client, compound)

    mocks = _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # No op_count-mismatch compound should ever reach add/remove etc.
    assert mocks["add_order"].call_count == 0
    assert mocks["remove_order"].call_count == 0
    assert mocks["pin_order"].call_count == 0
    assert mocks["unpin_order"].call_count == 0

    # WS notify fires with compound_failed + a clear message about the
    # mismatch. failed_op_index is -1 to signal "no specific op — the
    # whole compound was malformed".
    assert mocks["notify_user"].call_count == 1
    msg = mocks["notify_user"].call_args.kwargs["message"]
    assert msg["type"] == "schedule.compound_failed"
    assert msg["compound_id"] == str(failing_compound_id)
    assert msg["failed_op_index"] == -1
    assert msg["rolled_back"] is True
    assert "op_count" in (msg["detail"] or "")


def test_run_scheduling_pin_failure_rolls_back_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """A 1-op pin compound that fails triggers a rollback + WS notify.

    The next compound (a successful add) still runs on its own turn —
    compound failures are independent across compounds. WS payload type
    is the unified ``schedule.compound_failed`` (no more per-op
    ``schedule.pin_failed``), with ``failed_op="pin"`` in the detail.
    """
    pin_user = uuid.uuid4()
    pin_compound_id = uuid.uuid4()
    pin_compound = _make_op(
        op="pin",
        group="grow",
        order_number="PIN-FAIL",
        deadline="2026-05-15",
        fake_deadline="2026-05-12",
        requested_by=pin_user,
    )
    pin_compound["compound_id"] = str(pin_compound_id)
    follow_compound = _make_op(order_number="ORD-OK")
    _enqueue(redis_client, pin_compound)
    _enqueue(redis_client, follow_compound)

    pin_mock = MagicMock(
        return_value=ScheduleResult(
            status="capacity_exceeded",
            message="Need 1000 wafers, only 0 available.",
        )
    )
    mocks = _patch_common(monkeypatch)
    monkeypatch.setattr("app.workers.scheduling.pin_order", pin_mock)
    mocks["pin_order"] = pin_mock

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Pin compound rolled back; the follow-up add compound ran on its own turn.
    assert pin_mock.call_count == 1
    assert mocks["add_order"].call_count == 1

    # WS notify: schedule.compound_failed for the pin compound.
    failed_calls = [
        c
        for c in mocks["notify_user"].call_args_list
        if c.kwargs["message"]["type"] == "schedule.compound_failed"
    ]
    assert len(failed_calls) == 1
    payload = failed_calls[0].kwargs["message"]
    assert failed_calls[0].kwargs["user_id"] == pin_user
    assert payload["compound_id"] == str(pin_compound_id)
    assert payload["failed_op"] == "pin"
    assert payload["order_number"] == "PIN-FAIL"
    assert payload["reason"] == "capacity_exceeded"
    assert payload["rolled_back"] is True


def test_run_scheduling_dispatches_unpin_op(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """``op="unpin"`` calls ``unpin_order(state, order_id)`` — no fake_deadline
    needed. Confirms the unpin path doesn't accidentally route through pin or
    remove (a regression that would silently corrupt state).
    """
    target_id = uuid.uuid4()
    op = _make_op(
        op="unpin",
        group="shrink",
        order_id=target_id,
        order_number="UNPIN-ME",
        deadline="2026-05-20",
    )
    _enqueue(redis_client, op)

    mocks = _patch_common(monkeypatch)
    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    assert mocks["unpin_order"].call_count == 1
    # signature: unpin_order(state, order_id)
    args, _ = mocks["unpin_order"].call_args
    _, order_id_arg = args
    assert order_id_arg == target_id

    # Did NOT route to remove or pin.
    assert mocks["remove_order"].call_count == 0
    assert mocks["pin_order"].call_count == 0


def test_materialize_task_drains_pending_users_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Happy path: notify_pending has users → materializer renames it,
    runs apply_schedule, then notify_user(schedule.materialized) per user.
    """
    from app.workers.scheduling import materialize_schedule_task

    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    redis_client.sadd("schedule:materialize_notify_pending", str(user_a), str(user_b))

    monkeypatch.setattr(
        "app.workers.scheduling._load_state",
        lambda: SchedulerState.initial(date(2026, 5, 5)),
    )
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _s: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    apply_mock = MagicMock(return_value=0)
    monkeypatch.setattr("app.workers.scheduling.order_service.apply_schedule", apply_mock)
    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )

    result = materialize_schedule_task.apply()
    assert result.successful(), result.traceback

    # apply_schedule called exactly once.
    assert apply_mock.call_count == 1
    # Both users notified with schedule.materialized.
    notified = {
        c.kwargs["user_id"]: c.kwargs["message"]["type"] for c in notify_mock.call_args_list
    }
    assert notified == {
        user_a: "schedule.materialized",
        user_b: "schedule.materialized",
    }
    # Pending set drained.
    assert redis_client.scard("schedule:materialize_notify_pending") == 0
    # Running flag released.
    assert redis_client.get("schedule:materialize_running") is None


def test_materialize_task_exits_when_already_running(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Self-coalescing: if another materializer already claimed the
    running flag, this invocation exits immediately. No work done.
    """
    from app.workers.scheduling import materialize_schedule_task

    # Pre-claim the flag — simulating another materializer running.
    redis_client.set("schedule:materialize_running", "1", ex=300)
    redis_client.sadd("schedule:materialize_notify_pending", str(uuid.uuid4()))

    apply_mock = MagicMock(return_value=0)
    monkeypatch.setattr("app.workers.scheduling.order_service.apply_schedule", apply_mock)
    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )

    result = materialize_schedule_task.apply()
    assert result.successful(), result.traceback

    # Nothing was done; the other in-flight materializer owns the work.
    assert apply_mock.call_count == 0
    assert notify_mock.call_count == 0
    # Pending set untouched.
    assert redis_client.scard("schedule:materialize_notify_pending") == 1
    # The flag we pre-set is still there (we didn't clobber another runner's slot).
    assert redis_client.get("schedule:materialize_running") == "1"


def test_materialize_task_exits_when_no_pending_work(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Empty notify_pending → rename raises ResponseError → loop exits
    immediately. No apply_schedule, no notify. Running flag released.
    """
    from app.workers.scheduling import materialize_schedule_task

    # No SADD — notify_pending doesn't exist.

    apply_mock = MagicMock(return_value=0)
    monkeypatch.setattr("app.workers.scheduling.order_service.apply_schedule", apply_mock)
    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )

    result = materialize_schedule_task.apply()
    assert result.successful(), result.traceback

    assert apply_mock.call_count == 0
    assert notify_mock.call_count == 0
    # Flag was claimed but released by the finally.
    assert redis_client.get("schedule:materialize_running") is None


def test_materialize_task_passes_pinned_map_to_apply_schedule(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """The materializer derives ``{order_id: fake_deadline}`` from
    ``state.pinned_orders`` and threads it into ``apply_schedule`` so DB
    rows get ``is_pinned=true`` / ``pinned_production_date=fake_deadline``.

    Phase 4: this invariant moved from ``_finalize_run`` (which the fast
    task no longer calls) to ``materialize_schedule_task``. The test was
    renamed and re-targeted accordingly.
    """
    from app.services.scheduling import PinnedOrder
    from app.workers.scheduling import materialize_schedule_task

    pinned_id = uuid.uuid4()
    pinned_day = date(2026, 5, 12)
    requester = uuid.uuid4()

    # Seed the materializer's pending notify-user set so it has work to do.
    redis_client.sadd("schedule:materialize_notify_pending", str(requester))

    def fake_load_state() -> SchedulerState:
        s = SchedulerState.initial(date(2026, 5, 5))
        s.pinned_orders[pinned_id] = PinnedOrder(
            order_id=pinned_id,
            order_number="PINNED",
            wafer_quantity=500,
            deadline=date(2026, 5, 15),
            fake_deadline=pinned_day,
        )
        return s

    monkeypatch.setattr("app.workers.scheduling._load_state", fake_load_state)
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _s: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    apply_mock = MagicMock(return_value=0)
    monkeypatch.setattr("app.workers.scheduling.order_service.apply_schedule", apply_mock)
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )

    result = materialize_schedule_task.apply()
    assert result.successful(), result.traceback

    # apply_schedule was called with the pinned map (3rd positional arg).
    args, _ = apply_mock.call_args
    _, _scheduled_arg, pinned_arg = args
    assert pinned_arg == {pinned_id: pinned_day}
