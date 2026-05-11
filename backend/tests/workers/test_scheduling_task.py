"""Tests for ``app.workers.scheduling`` Celery tasks.

Every external dependency is mocked via ``monkeypatch`` — Redis is replaced
by a small in-memory fake, the SQLAlchemy session is a ``MagicMock``, and
the algorithm functions / WebSocket placeholders are patched at the worker
module's binding site so the body under test is exercised in isolation.

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

# ---------------------------------------------------------------------------
# Fake Redis
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of Redis the worker uses.

    Implements the string ops (``get`` / ``set`` / ``incr``) and the sorted-set
    ops (``zadd`` / ``zpopmin`` / ``zcard``) that ``run_scheduling_task`` and
    ``rebuild_schedule_task`` exercise.
    """

    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        # Sorted set: list of (score, member) kept sorted by score ascending,
        # ties broken by member lex order (matches real Redis semantics
        # closely enough for our tests).
        self._zsets: dict[str, list[tuple[float, str]]] = {}
        # Hash maps for secondary indexes (e.g. by-compound-id).
        self._hashes: dict[str, dict[str, str]] = {}

    # ----- String ops --------------------------------------------------------
    def get(self, key: str) -> str | None:
        return self._strings.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        # ``ex`` (TTL seconds) is accepted for API compatibility — tests
        # don't actually expire keys; callers that care about TTL behavior
        # should manually evict via ``delete``.
        self._strings[key] = value

    def incr(self, key: str) -> int:
        cur = int(self._strings.get(key, "0")) + 1
        self._strings[key] = str(cur)
        return cur

    def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            if self._strings.pop(key, None) is not None:
                n += 1
            if self._zsets.pop(key, None) is not None:
                n += 1
        return n

    # ----- Sorted-set ops ----------------------------------------------------
    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        bucket = self._zsets.setdefault(key, [])
        added = 0
        for member, score in mapping.items():
            existing = next((i for i, (_, m) in enumerate(bucket) if m == member), None)
            if existing is not None:
                bucket.pop(existing)
            else:
                added += 1
            insert_at = next(
                (i for i, (s, m) in enumerate(bucket) if (s, m) > (score, member)),
                len(bucket),
            )
            bucket.insert(insert_at, (score, member))
        return added

    def zpopmin(self, key: str, count: int = 1) -> list[tuple[str, float]]:
        bucket = self._zsets.get(key)
        if not bucket:
            return []
        out: list[tuple[str, float]] = []
        for _ in range(min(count, len(bucket))):
            score, member = bucket.pop(0)
            out.append((member, score))
        return out

    def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, []))

    # ----- Hash ops ----------------------------------------------------------
    def hset(self, key: str, field: str, value: str) -> int:
        bucket = self._hashes.setdefault(key, {})
        is_new = field not in bucket
        bucket[field] = value
        return 1 if is_new else 0

    def hdel(self, key: str, *fields: str) -> int:
        bucket = self._hashes.get(key)
        if bucket is None:
            return 0
        removed = 0
        for f in fields:
            if bucket.pop(f, None) is not None:
                removed += 1
        return removed

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)


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
) -> dict[str, Any]:
    """Build a compound dict that ``_enqueue`` can land in the fake redis.

    Defaults to a single-add compound for the most common test case. When
    ``group`` isn't given, infer from the first op (``shrink`` for
    remove/unpin, ``grow`` for add/pin).
    """
    if not ops:
        ops = [_make_leaf_op()]
    if group is None:
        first = ops[0]["op"]
        group = "shrink" if first in ("remove", "unpin") else "grow"
    return {
        "compound_id": str(compound_id or uuid.uuid4()),
        "group": group,
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


def _enqueue(fake_redis: _FakeRedis, compound: dict[str, Any]) -> None:
    """Enqueue *compound* into the fake sorted-set like the real producer.

    Mirrors ``schedule_queue.enqueue_compound``: bumps the seq counter,
    embeds it as ``_seq``, and ZADDs at the score computed by ``score_for_op``
    using the compound's group field.
    """
    from app.services.scheduling import (
        PENDING_OPS_SEQ_KEY,
        score_for_op,
    )

    group = compound["group"]
    seq = fake_redis.incr(PENDING_OPS_SEQ_KEY)
    payload = {**compound, "_seq": seq}
    fake_redis.zadd(PENDING_OPS_KEY, {json.dumps(payload): score_for_op(group=group, seq=seq)})


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
    *,
    add_order: MagicMock | None = None,
    compute_schedule: Any | None = None,
) -> dict[str, MagicMock]:
    """Stub out the side-effecting collaborators of ``run_scheduling_task``.

    Returns a dict of the installed mocks so individual tests can make
    assertions on them.
    """
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)

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

    return {
        "add_order": add_mock,
        "remove_order": remove_mock,
        "pin_order": pin_mock,
        "unpin_order": unpin_mock,
        "apply_schedule": apply_mock,
        "broadcast": broadcast_mock,
        "notify_user": notify_mock,
        "delay": delay_mock,
    }


# ---------------------------------------------------------------------------
# run_scheduling_task — happy path
# ---------------------------------------------------------------------------


def test_run_scheduling_processes_two_adds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = _FakeRedis()
    op1 = _make_op(order_number="ORD-001")
    op2 = _make_op(order_number="ORD-002", wafer_quantity=2000)
    # ZADD with monotonic seq: op1 has smaller score ⇒ ZPOPMIN'd first.
    _enqueue(fake_redis, op1)
    _enqueue(fake_redis, op2)

    mocks = _patch_common(monkeypatch, fake_redis)

    result = run_scheduling_task.apply()

    assert result.successful(), result.traceback
    # Per-op design: each invocation handles one op and re-triggers itself.
    # The test's auto-retrigger delay-side-effect bridges those calls so the
    # whole queue drains under a single test-driven apply().
    assert mocks["add_order"].call_count == 2
    # apply_schedule + broadcast fire once per op (per-op refresh signal).
    assert mocks["apply_schedule"].call_count == 2
    assert mocks["broadcast"].call_count == 2
    mocks["broadcast"].assert_called_with({"type": "schedule.updated"})
    # delay() fired once between op1 and op2; not after op2 because the
    # queue was empty.
    assert mocks["delay"].call_count == 1
    # Final status: idle, with a finished_at timestamp
    status_doc = json.loads(fake_redis.get(STATUS_KEY))
    assert status_doc["state"] == "idle"
    assert status_doc["finished_at"] is not None
    # State persisted
    assert fake_redis.get(STATE_KEY) is not None


# ---------------------------------------------------------------------------
# run_scheduling_task — capacity exceeded notifies the requester
# ---------------------------------------------------------------------------


def test_run_scheduling_notifies_user_on_capacity_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compound containing a failing ``add`` rolls back + WS-notifies.

    Compound model contract: an op-level failure inside a compound triggers
    a snapshot rollback and ``schedule.compound_failed`` to the compound's
    ``requested_by``. The successful op inside a separate compound runs
    normally on its own turn.
    """
    fake_redis = _FakeRedis()

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
    _enqueue(fake_redis, compound_fail)
    _enqueue(fake_redis, compound_ok)

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
    mocks = _patch_common(monkeypatch, fake_redis, add_order=add_mock)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Both compounds were popped; failed compound rolled back, successful
    # compound finalized normally.
    assert mocks["add_order"].call_count == 2

    # Exactly one notify_user — the failed compound's requester.
    assert mocks["notify_user"].call_count == 1
    kwargs = mocks["notify_user"].call_args.kwargs
    assert kwargs["user_id"] == failing_user
    msg = kwargs["message"]
    assert msg["type"] == "schedule.compound_failed"
    assert msg["compound_id"] == str(failing_compound_id)
    assert msg["failed_op"] == "add"
    assert msg["failed_op_index"] == 0
    assert msg["order_id"] == str(failing_id)
    assert msg["reason"] == "capacity_exceeded"
    assert msg["rolled_back"] is True


def test_run_scheduling_notifies_user_on_remove_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compound containing a failing ``remove`` rolls back + WS-notifies.

    Realistic trigger: a stale producer pushed a ``remove`` for an order
    that's no longer in the pq (e.g. it was pinned out, or already removed
    by a previous compound). The compound rolls back (no-op since remove
    was the only op) and the requester gets ``schedule.compound_failed``
    with ``failed_op="remove"`` so the UI can surface the inconsistency.
    """
    fake_redis = _FakeRedis()

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
    _enqueue(fake_redis, compound)

    remove_mock = MagicMock(
        return_value=ScheduleResult(
            status="deadline_too_far",
            order_id=failing_id,
            message="Deadline outside the 30-day scheduling horizon.",
        )
    )
    mocks = _patch_common(monkeypatch, fake_redis)
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
    fake_redis = _FakeRedis()
    _enqueue(fake_redis, _make_op(order_number="ORD-BOOM"))

    # add_order is the realistic crash point — bug in segment tree code
    # would manifest there. Any internal raise has the same contract.
    failing_add = MagicMock(side_effect=RuntimeError("segment tree corrupted"))
    mocks = _patch_common(monkeypatch, fake_redis, add_order=failing_add)

    result = run_scheduling_task.apply()
    # Celery sees the failure (traceback is in result.traceback).
    assert not result.successful()
    assert "segment tree corrupted" in (result.traceback or "")

    # Status doc shows the failure so operators see it via /schedule/status.
    raw = fake_redis.get(STATUS_KEY)
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
) -> None:
    fake_redis = _FakeRedis()
    _enqueue(fake_redis, _make_op())

    # Inject a "new" op after the drain by hooking compute_schedule.
    def fake_compute(_state: SchedulerState) -> list:
        _enqueue(fake_redis, _make_op(order_number="LATE"))
        return []

    mocks = _patch_common(monkeypatch, fake_redis, compute_schedule=fake_compute)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    assert mocks["delay"].called  # fired itself again


def test_run_scheduling_processes_shrink_group_before_grow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compound updates' ops must respect their group: shrink-group runs to
    completion before grow-group, regardless of the queue's RPOP order."""
    fake_redis = _FakeRedis()

    # Producer pushed in order: a defer (shrink remove + shrink add), then an
    # advance (grow remove + grow add). Worker should process all four shrink
    # ops first (in original order), then all four grow ops.
    defer_remove = _make_op(op="remove", group="shrink", order_number="DEFER-R")
    defer_add = _make_op(op="add", group="shrink", order_number="DEFER-A")
    advance_remove = _make_op(op="remove", group="grow", order_number="ADVANCE-R")
    advance_add = _make_op(op="add", group="grow", order_number="ADVANCE-A")
    for op in (defer_remove, defer_add, advance_remove, advance_add):
        _enqueue(fake_redis, op)

    call_order: list[str] = []

    def track_add(_state: SchedulerState, order: SchedulingOrder) -> ScheduleResult:
        call_order.append(f"add:{order.order_number}")
        return ScheduleResult(status="success")

    def track_remove(_state: SchedulerState, order: SchedulingOrder) -> ScheduleResult:
        call_order.append(f"remove:{order.order_number}")
        return ScheduleResult(status="success")

    monkeypatch.setattr("app.workers.scheduling.add_order", MagicMock(side_effect=track_add))
    monkeypatch.setattr("app.workers.scheduling.remove_order", MagicMock(side_effect=track_remove))
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _s: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
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
) -> None:
    """A shrink op LPUSH'd while a grow batch is being processed must be
    picked up *before* the remaining grow ops, not after them.

    Setup: queue starts with two grow ops [GROW-1 (older), GROW-2]. A side
    effect on the *first* grow's add_order injects a fresh shrink op into
    the queue. The next pop must therefore see the new shrink and run it
    before GROW-2."""
    fake_redis = _FakeRedis()
    _enqueue(fake_redis, _make_op(op="add", group="grow", order_number="GROW-1"))
    _enqueue(fake_redis, _make_op(op="add", group="grow", order_number="GROW-2"))

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
                fake_redis,
                _make_op(op="remove", group="shrink", order_number="LATE-SHRINK"),
            )
        return ScheduleResult(status="success")

    monkeypatch.setattr("app.workers.scheduling.add_order", MagicMock(side_effect=track_add))
    monkeypatch.setattr("app.workers.scheduling.remove_order", MagicMock(side_effect=track_remove))
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _s: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
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
) -> None:
    fake_redis = _FakeRedis()
    # No pending ops at all.
    mocks = _patch_common(monkeypatch, fake_redis)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    assert mocks["add_order"].call_count == 0
    assert not mocks["delay"].called


def test_run_scheduling_yields_retrigger_to_waiter(
    monkeypatch: pytest.MonkeyPatch,
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
    fake_redis = _FakeRedis()
    # Pre-set the waiter flag: a waiter is waiting on us right now.
    fake_redis.set("schedule:waiter_pending", "1", ex=600)
    # One op processed + one still queued = zcard > 0 at end of task →
    # would normally fire delay() if not for the yield.
    _enqueue(fake_redis, _make_op(order_number="ORD-A"))
    _enqueue(fake_redis, _make_op(order_number="ORD-B"))

    # Plain delay (no auto-retrigger) — we want to verify it's NOT called.
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
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
    plain_delay = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", plain_delay)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # First op was processed (per-op design)…
    assert add_mock.call_count == 1
    # …second op still pending (zcard > 0)…
    assert fake_redis.zcard(PENDING_OPS_KEY) == 1
    # …but delay was NOT fired because the waiter holds responsibility for
    # the next re-trigger.
    assert plain_delay.call_count == 0


# ---------------------------------------------------------------------------
# Waiter flag — advance_day / rebuild set it, finally clears it
# ---------------------------------------------------------------------------


def test_advance_day_sets_waiter_flag_then_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """advance_day_task must hold the waiter flag for the duration of its
    body so a concurrent ``run_scheduling_task`` finishing during the wait
    yields its re-trigger to us. Cleared in ``finally`` so a clean run
    leaves the flag unset for future re-triggers."""
    fake_redis = _FakeRedis()
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
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
        flag_during.append(fake_redis.get("schedule:waiter_pending"))
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
    assert fake_redis.get("schedule:waiter_pending") is None


def test_advance_day_clears_waiter_flag_even_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the waiter body crashes mid-flight the flag MUST still be cleared,
    otherwise future ``run_scheduling_task`` invocations would yield to a
    phantom waiter forever (until TTL expires, which is too long to wait).

    Guarded by the ``finally`` clause around the body.
    """
    fake_redis = _FakeRedis()
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
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
    assert fake_redis.get("schedule:waiter_pending") is None


def test_rebuild_clears_waiter_flag_even_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror test for ``rebuild_schedule_task``: if any step raises (e.g.
    DB layer down, list_for_scheduler errors), the waiter flag still gets
    cleared so the system recovers without waiting for TTL expiry."""
    fake_redis = _FakeRedis()
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
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
    assert fake_redis.get("schedule:waiter_pending") is None


# ---------------------------------------------------------------------------
# Status-claim — advance_day / rebuild own schedule:status while working
# ---------------------------------------------------------------------------


def test_advance_day_claims_status_running_during_body_and_clears_to_idle(
    monkeypatch: pytest.MonkeyPatch,
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
    fake_redis = _FakeRedis()
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
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
        raw = fake_redis.get("schedule:status")
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
    raw = fake_redis.get("schedule:status")
    assert raw is not None
    final = json.loads(raw)
    assert final["state"] == "idle"
    assert final["finished_at"] is not None


def test_advance_day_writes_status_failed_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the body raises after status was claimed, status MUST flip to
    ``failed`` (with the error captured) — NOT ``idle``. Writing ``idle``
    after a real failure makes ``GET /schedule/status`` show a healthy
    scheduler and silently masks the broken run from operators.

    Also asserts status doesn't stick at ``running`` (that would 409 every
    future ``/trigger``). The acceptable terminal states on exception are
    ``failed`` (visible to ops) — never ``running`` and never ``idle``.
    """
    fake_redis = _FakeRedis()
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
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

    raw = fake_redis.get("schedule:status")
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
) -> None:
    """``advance_day_task`` must wait for any in-flight run, advance the
    horizon, finalize (compute → apply → save → broadcast) directly, and
    only then re-trigger ``run_scheduling_task`` if pending ops remain.

    Pre-seed one pending op so we can verify the conditional retrigger; the
    no-pending-ops branch is implicitly covered by checking call count
    against the ``zcard`` decision.
    """
    fake_redis = _FakeRedis()
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)

    # One pending op so the conditional retrigger fires.
    _enqueue(fake_redis, _make_op(order_number="POST-ADVANCE"))

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
    fake_redis = _FakeRedis()
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
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
    new_state.priority_queue.append(
        SchedulingOrder(
            order_id=order_y,
            order_number="ORD-Y",
            wafer_quantity=1000,
            deadline=tomorrow + timedelta(days=2),
        )
    )

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
            [
                ScheduledResult(
                    order_id=order_y, scheduled_date=tomorrow, quantity=1000
                )
            ],
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
) -> None:
    """Full happy path: status flips from running→idle while rebuild_task is
    polling, then it rebuilds, persists, and re-triggers run_scheduling_task.

    No skipped orders in this path — the next test covers that branch.
    """
    fake_redis = _FakeRedis()
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)

    base = date(2026, 5, 5)
    fake_redis.set(STATE_KEY, SchedulerState.initial(base).to_json())
    # One pending op so the conditional retrigger fires after rebuild.
    _enqueue(fake_redis, _make_op(order_number="POST-REBUILD"))

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
    rebuilt_state.priority_queue.append(pulled_order)
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
    saved_raw = fake_redis.get(STATE_KEY)
    assert saved_raw is not None
    assert saved_raw == rebuilt_state.to_json()
    # No skipped → no notify_user calls.
    notify_mock.assert_not_called()
    # run_scheduling_task was kicked off because POST-REBUILD is pending.
    assert delay_mock.called


def test_rebuild_task_notifies_each_skipped_orders_creator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``rebuild_state`` returns a non-empty ``skipped`` list, the task
    must push a ``schedule.rebuild_skipped`` WebSocket message to each
    skipped order's creator (looked up via the ``creators`` map). Skipped
    orders without a known creator are silently dropped (defensive)."""
    fake_redis = _FakeRedis()
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)

    base = date(2026, 5, 5)
    fake_redis.set(STATE_KEY, SchedulerState.initial(base).to_json())

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
) -> None:
    """If Redis has no ``schedule:state`` (first deploy / wiped), rebuild
    falls back to ``datetime.now().date()`` as base_date."""
    fake_redis = _FakeRedis()
    # Note: do NOT pre-seed STATE_KEY.
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
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
) -> None:
    """A queued ``op="pin"`` must reach ``pin_order(state, order, fake_deadline)``
    with the fake_deadline parsed back into a ``date``. This is the contract
    that lets the API encode pin requests as a normal ScheduleOperationRequest.
    """
    fake_redis = _FakeRedis()
    op = _make_op(
        op="pin",
        group="grow",
        order_number="PIN-ME",
        deadline="2026-05-15",
        fake_deadline="2026-05-12",
    )
    _enqueue(fake_redis, op)

    mocks = _patch_common(monkeypatch, fake_redis)
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

    fake_redis = _FakeRedis()

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
    fake_redis.set(STATE_KEY, state.to_json())

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
    _enqueue(fake_redis, compound)

    # No mocks for add/remove — we want the REAL algorithm to fail on the
    # huge qty, so we can observe true state rollback.
    monkeypatch.setattr("app.workers.scheduling._get_redis", lambda: fake_redis)
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
    saved_raw = fake_redis.get(STATE_KEY)
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


def test_run_scheduling_pin_failure_rolls_back_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1-op pin compound that fails triggers a rollback + WS notify.

    The next compound (a successful add) still runs on its own turn —
    compound failures are independent across compounds. WS payload type
    is the unified ``schedule.compound_failed`` (no more per-op
    ``schedule.pin_failed``), with ``failed_op="pin"`` in the detail.
    """
    fake_redis = _FakeRedis()
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
    _enqueue(fake_redis, pin_compound)
    _enqueue(fake_redis, follow_compound)

    pin_mock = MagicMock(
        return_value=ScheduleResult(
            status="capacity_exceeded",
            message="Need 1000 wafers, only 0 available.",
        )
    )
    mocks = _patch_common(monkeypatch, fake_redis)
    monkeypatch.setattr("app.workers.scheduling.pin_order", pin_mock)
    mocks["pin_order"] = pin_mock

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Pin compound rolled back; the follow-up add compound ran on its own turn.
    assert pin_mock.call_count == 1
    assert mocks["add_order"].call_count == 1

    # WS notify: schedule.compound_failed for the pin compound.
    failed_calls = [
        c for c in mocks["notify_user"].call_args_list
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
) -> None:
    """``op="unpin"`` calls ``unpin_order(state, order_id)`` — no fake_deadline
    needed. Confirms the unpin path doesn't accidentally route through pin or
    remove (a regression that would silently corrupt state).
    """
    fake_redis = _FakeRedis()
    target_id = uuid.uuid4()
    op = _make_op(
        op="unpin",
        group="shrink",
        order_id=target_id,
        order_number="UNPIN-ME",
        deadline="2026-05-20",
    )
    _enqueue(fake_redis, op)

    mocks = _patch_common(monkeypatch, fake_redis)
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


def test_finalize_run_passes_pinned_map_to_apply_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_finalize_run`` derives ``{order_id: fake_deadline}`` from
    ``state.pinned_orders`` and threads it into ``apply_schedule`` so DB
    rows get ``is_pinned=true`` / ``pinned_production_date=fake_deadline``.

    Pre-fix apply_schedule was called with positional ``scheduled`` only,
    so even with the new ``pinned`` parameter the worker had to be wired
    to actually pass it. This locks that wiring.
    """
    from app.services.scheduling import PinnedOrder

    fake_redis = _FakeRedis()
    _enqueue(fake_redis, _make_op(order_number="GROW-1"))

    pinned_id = uuid.uuid4()
    pinned_day = date(2026, 5, 12)

    def fake_load_state() -> SchedulerState:
        s = SchedulerState.initial(date(2026, 5, 5))
        s.pinned_orders.append(
            PinnedOrder(
                order_id=pinned_id,
                order_number="PINNED",
                wafer_quantity=500,
                deadline=date(2026, 5, 15),
                fake_deadline=pinned_day,
            )
        )
        return s

    monkeypatch.setattr("app.workers.scheduling._load_state", fake_load_state)
    mocks = _patch_common(monkeypatch, fake_redis)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # apply_schedule was called with the pinned map (3rd positional arg).
    args, _ = mocks["apply_schedule"].call_args
    _, _scheduled_arg, pinned_arg = args
    assert pinned_arg == {pinned_id: pinned_day}
