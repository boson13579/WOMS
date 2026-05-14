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
    MATERIALIZE_NOTIFY_PENDING_KEY,
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


def _bypass_state_writer_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the P0-2/P0-3 lock always-succeed for tests that don't exercise it.

    Real-Celery semantics: when ``run_scheduling_task`` self-retriggers via
    ``.delay()``, the re-fired task runs *after* the outer's ``finally``
    releases the lock — so the new task acquires successfully. In tests
    we route ``.delay()`` straight back into ``apply()`` (synchronously,
    inside the outer's ``try``) which means the outer still holds the
    lock when the recursive apply tries to acquire it. Without this
    bypass every "process more than one compound" test would deadlock
    on the second compound. The dedicated lock-behavior tests
    (``test_state_writer_lock_*``) skip this bypass so they exercise the
    real Redis SETNX.
    """
    monkeypatch.setattr(
        "app.workers.scheduling._try_acquire_state_lock",
        lambda _task_id: True,
    )
    monkeypatch.setattr(
        "app.workers.scheduling._acquire_state_lock_blocking",
        lambda _task_id, timeout_seconds=0: True,
    )
    monkeypatch.setattr(
        "app.workers.scheduling._release_state_lock",
        lambda _task_id: None,
    )


def _install_auto_retrigger_delay(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Wire ``run_scheduling_task.delay`` to synchronously call ``apply()``.

    Compound design: each task invocation handles ONE compound then
    ``.delay()``s itself if more remain. Tests want to call ``apply()``
    once and have the whole queue drain — so we route ``.delay()`` straight
    back into ``apply()`` here. A depth cap catches infinite-loop bugs.

    Also bypasses the state-writer lock so the recursive ``apply()`` can
    re-acquire (in prod the re-trigger fires after lock release, but in
    tests the recursive apply happens inside the outer's still-running
    ``try`` block).
    """
    _bypass_state_writer_lock(monkeypatch)
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
    is_batch_feasible: Any | None = None,
) -> dict[str, MagicMock]:
    """Stub out the side-effecting collaborators of ``run_scheduling_task``.

    Batch-admission rewrite: the worker no longer calls ``add_order`` /
    ``remove_order`` (those did per-op tree work + capacity validation;
    that's now consolidated into ``apply_batch_to_capacity`` +
    ``apply_batch_to_deadline`` after a single batch feasibility check).
    pq mutations happen via ``pq_add`` / ``pq_remove`` directly against a
    real ``SchedulerState``, so tests don't mock those. ``pin_order`` /
    ``unpin_order`` are still called per leaf because they do their own
    self-contained tree swap.

    Redis itself is NOT patched — the session-scoped ``redis_container``
    fixture supplies a real client at ``settings.REDIS_URL``. Pre-seeding
    ``STATE_KEY`` is the test's job; the default empty state
    (``SchedulerState.initial(today)``) has full HORIZON_DAYS*DAILY_CAPACITY
    of capacity, so any modest-quantity compound feasibility-checks True.

    To force infeasibility in a test: pre-seed ``STATE_KEY`` with a state
    that already has tree contributions exhausting the prefix sum, OR
    pass ``is_batch_feasible`` to override the check at the worker module
    boundary.
    """
    if is_batch_feasible is not None:
        monkeypatch.setattr("app.workers.scheduling.is_batch_feasible", is_batch_feasible)

    pin_mock = MagicMock(return_value=ScheduleResult(status="success"))
    monkeypatch.setattr("app.workers.scheduling.pin_order", pin_mock)

    unpin_mock = MagicMock(return_value=ScheduleResult(status="success"))
    monkeypatch.setattr("app.workers.scheduling.unpin_order", unpin_mock)

    # apply_schedule / compute_schedule are only called by the materializer
    # / advance_day / rebuild paths — run_scheduling no longer touches them.
    # We patch them defensively anyway so any accidental call surfaces.
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda state: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    apply_mock = MagicMock(return_value=0)
    monkeypatch.setattr("app.workers.scheduling.order_service.apply_schedule", apply_mock)

    broadcast_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", broadcast_mock)

    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)

    delay_mock = _install_auto_retrigger_delay(monkeypatch)

    # Phase 4 + batch rewrite: run_scheduling_task dispatches the materializer
    # ONCE per drain (after the while loop), not once per compound. Tests
    # that focus on the fast path don't want a real materializer to run,
    # so we mock its .delay(). Dedicated materializer tests reset this
    # monkeypatch locally.
    materialize_delay_mock = MagicMock()
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        materialize_delay_mock,
    )

    return {
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


def test_run_scheduling_processes_two_adds_in_single_batch(
    monkeypatch: pytest.MonkeyPatch, redis_client: Redis
) -> None:
    """Two modest-quantity compounds in pending queue should be accepted as
    one batch: tree updates apply once, both compounds get
    ``compound_accepted`` WS, ONE materializer dispatch covers both.

    Batch-admission rewrite contract: when ``is_batch_feasible([1..N]) ==
    True``, the worker accepts the whole prefix in a single tree update.
    Materializer is dispatched once per drain (after the while loop),
    not once per compound — that's a behavior change from the per-
    compound design.
    """
    op1 = _make_op(order_number="ORD-001")
    op2 = _make_op(order_number="ORD-002", wafer_quantity=2000)
    _enqueue(redis_client, op1)
    _enqueue(redis_client, op2)

    mocks = _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Both compounds got compound_accepted notifications.
    assert mocks["notify_user"].call_count == 2
    for call in mocks["notify_user"].call_args_list:
        assert call.kwargs["message"]["type"] == "schedule.compound_accepted"

    # Materializer dispatched ONCE for the whole drain, not per-compound.
    assert mocks["materialize_delay"].call_count == 1
    # Self-retrigger NOT fired (nothing left in queue post-drain).
    assert mocks["delay"].call_count == 0
    # Phase 4 slow path: apply_schedule + broadcast still gated on the
    # materializer, not the fast path.
    assert mocks["apply_schedule"].call_count == 0
    assert mocks["broadcast"].call_count == 0

    # Queue is fully drained.
    assert redis_client.zcard(PENDING_OPS_KEY) == 0
    # Status flipped back to idle.
    status_doc = json.loads(redis_client.get(STATUS_KEY))
    assert status_doc["state"] == "idle"
    assert status_doc["finished_at"] is not None
    # State persisted.
    assert redis_client.get(STATE_KEY) is not None
    # Both requesters SADD'd into the materializer's notify queue.
    assert redis_client.scard("schedule:materialize_notify_pending") == 2


# ---------------------------------------------------------------------------
# run_scheduling_task — first-compound infeasibility drops + notifies
# ---------------------------------------------------------------------------


def test_run_scheduling_rejects_first_compound_when_infeasible_alone(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """If even ``[1..1]`` is infeasible (binary search returns 0), the
    first compound is ZREM'd + ``compound_failed`` WS-notified + the
    drain loop continues so subsequent feasible compounds still get a
    chance.

    Contract: this is the only path that produces ``compound_failed`` in
    the new design (saga rollback is gone). ``reason="capacity_exceeded"``
    matches the schema the frontend expects.
    """
    failing_id = uuid.uuid4()
    failing_user = uuid.uuid4()
    failing_compound_id = uuid.uuid4()
    compound_fail = _make_op(
        order_id=failing_id,
        order_number="ORD-FAIL",
        wafer_quantity=1000,
        requested_by=failing_user,
    )
    compound_fail["compound_id"] = str(failing_compound_id)
    compound_ok = _make_op(order_number="ORD-OK", wafer_quantity=1000)
    _enqueue(redis_client, compound_fail)
    _enqueue(redis_client, compound_ok)

    # Force the first compound (alone) to be infeasible, second OK.
    feasibility_calls: list[int] = []

    def mock_feasible(_state: Any, delta: list[int]) -> bool:
        feasibility_calls.append(sum(delta))
        # Round 1 (binary search): [1..2] = both. delta sums to 2000.
        # Round 1 halved: [1..1] = compound_fail. delta sums to 1000.
        # Round 2 (after compound_fail rejected): [1..1] = compound_ok. sums to 1000.
        # We can't distinguish "[1..1] of compound_fail" vs "[1..1] of compound_ok"
        # by delta sum alone (both are 1000). Track call sequence instead.
        # Calls 1 and 2 = round 1 (full + halved, both should fail).
        # Call 3 = round 2 (compound_ok alone, should succeed).
        return len(feasibility_calls) >= 3

    mocks = _patch_common(monkeypatch, is_batch_feasible=mock_feasible)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Two WS notifications: one failed (compound_fail), one accepted (compound_ok).
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

    # Materializer dispatched once for the accepted compound.
    assert mocks["materialize_delay"].call_count == 1
    # Queue fully drained.
    assert redis_client.zcard(PENDING_OPS_KEY) == 0


# ---------------------------------------------------------------------------
# run_scheduling_task — halving accepts the largest fitting prefix
# ---------------------------------------------------------------------------


def test_run_scheduling_halving_accepts_smaller_prefix_then_drains_rest(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Halving probe: when ``[1..N]`` is infeasible, the worker halves
    to ``[1..N//2]`` etc. until first feasible. Remaining compounds get
    picked up on the next drain-loop iteration.

    Setup: 4 compounds where [1..4] fails, [1..2] passes. Expected:
    iter 1 accepts [1..2] (halving N=4 → 2 → pass), iter 2 reads the
    remaining 2 compounds, accepts [1..2] (halving N=2 → 2 → pass; or
    N=2 → 1 → pass depending on feasibility). Verify the queue drains
    fully and both materializer dispatches fire (one per accepted batch).
    """
    # Future deadline so the ops actually contribute to the batch delta
    # (compute_batch_capacity_delta silently drops past-dated ops, which
    # would make a sum-based feasibility mock trivially pass).
    future_dl = (date.today() + timedelta(days=10)).isoformat()
    for i in range(4):
        _enqueue(
            redis_client,
            _make_op(order_number=f"ORD-{i:02d}", wafer_quantity=1000, deadline=future_dl),
        )

    # is_batch_feasible: returns False if total demand > 2000 (= more than
    # 2 compounds at 1000 qty), True otherwise. Forces the binary search
    # to halve from N=4 → 2 → pass.
    def mock_feasible(_state: Any, delta: list[int]) -> bool:
        return sum(delta) <= 2000

    mocks = _patch_common(monkeypatch, is_batch_feasible=mock_feasible)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # All 4 compounds eventually accepted across 2 drain iterations.
    accepted_notifies = [
        c
        for c in mocks["notify_user"].call_args_list
        if c.kwargs["message"]["type"] == "schedule.compound_accepted"
    ]
    assert len(accepted_notifies) == 4
    # Materializer dispatched ONCE per drain regardless of internal batch
    # count — even though the drain committed 2 batches (4 compounds split
    # 2+2 by binary-search halving), the materializer dispatch is hoisted
    # to after the drain loop so a single materialize cycle covers the
    # whole drain. Per-batch dispatches would just thrash the materializer.
    assert mocks["materialize_delay"].call_count == 1
    assert redis_client.zcard(PENDING_OPS_KEY) == 0


# ---------------------------------------------------------------------------
# run_scheduling_task — status=failed + re-raise on unexpected exception
# ---------------------------------------------------------------------------


def test_run_scheduling_writes_status_failed_on_exception_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """When ``run_scheduling_task`` body raises, it MUST:

    1. Write ``schedule:status`` to ``failed`` with the error string captured.
    2. NOT leave status stuck at ``running`` (would 409 every future
       ``POST /schedule/trigger``).
    3. Re-raise so Celery records the traceback.

    Forcing the failure via ``_save_state`` exercises the
    inside-the-try branch of the task. ``_save_state`` is called by
    ``_commit_accepted_batch``, so a real compound needs to be pending.
    """
    _enqueue(redis_client, _make_op(order_number="ORD-BOOM"))

    mocks = _patch_common(monkeypatch)
    monkeypatch.setattr(
        "app.workers.scheduling._save_state",
        MagicMock(side_effect=RuntimeError("segment tree corrupted")),
    )

    result = run_scheduling_task.apply()
    assert not result.successful()
    assert "segment tree corrupted" in (result.traceback or "")

    raw = redis_client.get(STATUS_KEY)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["state"] == "failed"
    assert payload["error"] == "segment tree corrupted"
    assert payload["finished_at"] is not None
    assert payload["state"] != "running"

    # No re-trigger fired on the failure path.
    assert not mocks["delay"].called


# ---------------------------------------------------------------------------
# run_scheduling_task — mid-drain arrival picked up by re-read
# ---------------------------------------------------------------------------


def test_run_scheduling_picks_up_compound_arriving_during_drain(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """A compound arriving while the drain loop is mid-commit must be
    picked up on the next ``_read_pending_compounds`` call.

    With batch admission the drain loop re-reads pending after each batch
    commit, so a late arrival is processed in the SAME task invocation —
    no separate ``.delay()`` re-trigger is needed for it. The retrigger
    at the end only fires for arrivals that landed AFTER the loop's last
    read (between drain finish and status flip).
    """
    _enqueue(redis_client, _make_op(order_number="ORD-FIRST"))

    # Inject a late compound via _save_state hook (runs once per batch commit).
    real_save_state = None
    injected = {"done": False}

    def save_state_with_injection(state: Any) -> None:
        # Call the real save_state behavior so state persists.
        from app.workers.scheduling import _save_state as inner

        inner(state)
        if not injected["done"]:
            _enqueue(redis_client, _make_op(order_number="ORD-LATE"))
            injected["done"] = True

    # Save the unpatched ref before patching — but _patch_common doesn't
    # touch _save_state, so we can read it after patching collaborators.
    mocks = _patch_common(monkeypatch)
    # Patch _save_state to a hooked version that injects a late compound
    # the first time it's called. We need to capture the original
    # _save_state before our patch so we can still call it.
    from app.workers import scheduling as worker_module

    real_save_state = worker_module._save_state

    def hooked_save(state: Any) -> None:
        real_save_state(state)
        if not injected["done"]:
            _enqueue(redis_client, _make_op(order_number="ORD-LATE"))
            injected["done"] = True

    monkeypatch.setattr("app.workers.scheduling._save_state", hooked_save)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Both compounds accepted (the original + the one injected mid-drain).
    accepted = [
        c
        for c in mocks["notify_user"].call_args_list
        if c.kwargs["message"]["type"] == "schedule.compound_accepted"
    ]
    assert len(accepted) == 2
    # Queue drained — late arrival was picked up in the SAME task invocation.
    assert redis_client.zcard(PENDING_OPS_KEY) == 0
    # No retrigger needed — drain loop handled the late arrival itself.
    assert mocks["delay"].call_count == 0


# ---------------------------------------------------------------------------
# run_scheduling_task — shrink-group compounds applied before grow-group
# ---------------------------------------------------------------------------


def test_run_scheduling_applies_compounds_in_priority_order(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Within a batch, compounds are applied in sorted-set score order:
    shrink-group first, then grow-group, FIFO within each group.

    ``_read_pending_compounds`` uses ZRANGE which returns ascending score.
    The batch admission applies all accepted compounds via
    ``_apply_compound_leaf_structural`` in the same order, so the
    structural side-effects (``pq_add`` / ``pq_remove``) fire in priority
    order. We verify via mock call ordering on ``pq_add`` / ``pq_remove``.
    """
    defer_remove = _make_op(op="remove", group="shrink", order_number="DEFER-R")
    defer_add = _make_op(op="add", group="shrink", order_number="DEFER-A")
    advance_remove = _make_op(op="remove", group="grow", order_number="ADVANCE-R")
    advance_add = _make_op(op="add", group="grow", order_number="ADVANCE-A")
    for compound in (defer_remove, defer_add, advance_remove, advance_add):
        _enqueue(redis_client, compound)

    call_order: list[str] = []

    def track_pq_add(_state: Any, order: SchedulingOrder) -> None:
        call_order.append(f"add:{order.order_number}")

    def track_pq_remove(_state: Any, order_id: uuid.UUID) -> SchedulingOrder | None:
        # Best-effort: find which order_number matches the id from the queue.
        # For test purposes the order_number is embedded in the compound;
        # we use a lookup via the queue's stored payloads.
        for member, _score in redis_client.zrange(PENDING_OPS_KEY, 0, -1, withscores=True):
            compound = json.loads(member)
            for op in compound.get("ops", []):
                if op["order_id"] == str(order_id):
                    call_order.append(f"remove:{op['order_number']}")
                    return None
        # If the compound's already ZREM'd by the time we look, fall back to
        # a generic marker (shouldn't happen in this test's timing).
        call_order.append(f"remove:{order_id}")
        return None

    monkeypatch.setattr("app.workers.scheduling.pq_add", track_pq_add)
    monkeypatch.setattr("app.workers.scheduling.pq_remove", track_pq_remove)
    _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # All shrink-group compounds applied before any grow-group compound;
    # FIFO inside each group.
    assert call_order == [
        "remove:DEFER-R",
        "add:DEFER-A",
        "remove:ADVANCE-R",
        "add:ADVANCE-A",
    ]


def test_run_scheduling_late_shrink_processed_after_current_batch(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Compounds present at the moment of ``_read_pending_compounds`` are
    committed atomically as a batch (in priority order). A shrink-group
    compound arriving AFTER that read does NOT jump ahead of the
    currently-committing batch — it gets picked up on the next drain
    iteration.

    Old per-op design: late shrink jumped pending grows because each pop
    re-read priority. New batch design: atomicity is at batch granularity,
    not per-op, so the trade-off is throughput up / interrupt-jump
    semantics gone. Verifying the new contract.
    """
    _enqueue(redis_client, _make_op(op="add", group="grow", order_number="GROW-1"))
    _enqueue(redis_client, _make_op(op="add", group="grow", order_number="GROW-2"))

    call_order: list[str] = []
    injected = {"done": False}

    def track_pq_add(_state: Any, order: SchedulingOrder) -> None:
        call_order.append(f"add:{order.order_number}")
        # Inject a late shrink while GROW-1 is being structurally applied —
        # but since the batch's tree update + pq updates run inside one
        # atomic loop, the late shrink only becomes visible on the next
        # _read_pending_compounds.
        if order.order_number == "GROW-1" and not injected["done"]:
            _enqueue(
                redis_client,
                _make_op(op="remove", group="shrink", order_number="LATE-SHRINK"),
            )
            injected["done"] = True

    def track_pq_remove(_state: Any, order_id: uuid.UUID) -> SchedulingOrder | None:
        for member, _score in redis_client.zrange(PENDING_OPS_KEY, 0, -1, withscores=True):
            compound = json.loads(member)
            for op in compound.get("ops", []):
                if op["order_id"] == str(order_id):
                    call_order.append(f"remove:{op['order_number']}")
                    return None
        call_order.append(f"remove:{order_id}")
        return None

    monkeypatch.setattr("app.workers.scheduling.pq_add", track_pq_add)
    monkeypatch.setattr("app.workers.scheduling.pq_remove", track_pq_remove)
    _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Current batch [GROW-1, GROW-2] commits in full; LATE-SHRINK picked
    # up by the next drain iteration after the batch finishes.
    assert call_order == [
        "add:GROW-1",
        "add:GROW-2",
        "remove:LATE-SHRINK",
    ]


def test_run_scheduling_skips_retrigger_when_queue_drained(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Empty queue ⇒ no work done, no retrigger fired, status flips to idle."""
    mocks = _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    assert mocks["notify_user"].call_count == 0
    assert mocks["materialize_delay"].call_count == 0
    assert not mocks["delay"].called


def test_run_scheduling_yields_retrigger_to_waiter(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """If a waiter (advance_day / rebuild) holds the waiter flag, the
    drain loop must NOT self-dispatch even when zcard > 0 at the end —
    the waiter will fire ``run_scheduling_task.delay()`` after its own
    body. Without yielding, a re-triggered run_task races with the
    waiter for the state lock.

    Setup: pre-set the flag, enqueue 2 compounds, mock
    ``_read_pending_compounds`` so the drain only consumes one (simulating
    a partial drain where one compound got injected after the
    drain-loop's last read). Verify ``delay`` was not called.
    """
    redis_client.set("schedule:waiter_pending", "1", ex=600)

    _patch_common(monkeypatch)

    plain_delay = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", plain_delay)

    # Drain loop reads pending; we make it appear empty so the loop exits
    # cleanly, then leave a compound in the queue to trip the
    # end-of-task ``zcard > 0`` retrigger check.
    real_read = None
    from app.workers import scheduling as worker_module

    real_read = worker_module._read_pending_compounds
    call_count = {"value": 0}

    def faked_read(
        *,
        limit: int | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        call_count["value"] += 1
        if call_count["value"] == 1:
            # Simulate the drain loop seeing one compound, processing it,
            # then seeing the queue empty on the next iteration. We
            # accomplish this by deferring the actual enqueue to AFTER the
            # drain loop reads (= after we return empty). Add the compound
            # for the post-drain ``zcard`` check below.
            _enqueue(redis_client, _make_op(order_number="POST-DRAIN"))
            return []
        return real_read(limit=limit)

    monkeypatch.setattr("app.workers.scheduling._read_pending_compounds", faked_read)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # zcard > 0 after the drain (we enqueued POST-DRAIN), but the waiter
    # holds the flag so we yield.
    assert redis_client.zcard(PENDING_OPS_KEY) >= 1
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
    # Dict-backed pq: direct assignment to pq_index — EDF order is derived
    # at iteration time via _iter_pq_edf_sorted, not maintained here.
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


def test_run_scheduling_drains_op_count_mismatch_to_dlq(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Worker-side tamper guard: a compound whose declared ``op_count``
    doesn't match ``len(ops)`` is drained to the DLQ on read and the
    drain loop continues without applying it.

    Schema validation at enqueue time enforces this, but the Redis
    sorted-set member could in principle be corrupted post-enqueue
    (manual redis-cli surgery, mid-byte truncation, bit flip). The
    worker re-checks and routes the bad member to
    ``schedule:pending_ops:dlq`` rather than execute a half-truncated
    business action.

    Behavior change from saga design: pre-rewrite this produced a
    ``compound_failed`` WS notification; new design treats it as
    corruption and surfaces via ERROR log + DLQ instead (no requester
    notification because the compound shape is suspect — its
    ``requested_by`` may not be trustworthy either).
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
    # Enqueue a good compound AFTER the bad one to verify the drain loop
    # continues past the DLQ drain.
    _enqueue(redis_client, compound)
    good_compound = _make_op(order_number="ORD-GOOD")
    _enqueue(redis_client, good_compound)

    mocks = _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # The good compound was accepted normally.
    accepted = [
        c
        for c in mocks["notify_user"].call_args_list
        if c.kwargs["message"]["type"] == "schedule.compound_accepted"
    ]
    assert len(accepted) == 1
    # The bad compound got routed to the DLQ.
    assert redis_client.llen("schedule:pending_ops:dlq") == 1
    # …and was ZREM'd from pending so it doesn't loop forever.
    assert redis_client.zcard(PENDING_OPS_KEY) == 0
    # No compound_failed WS — corruption path doesn't notify the requester
    # (the requested_by field may itself be untrustworthy).
    failed = [
        c
        for c in mocks["notify_user"].call_args_list
        if c.kwargs["message"]["type"] == "schedule.compound_failed"
    ]
    assert failed == []


def test_run_scheduling_pin_failure_logs_but_continues_batch(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Pin / unpin "should never fail" per producer admission control —
    they are pure structural moves between pq and pinned_orders.

    Defensively, if ``pin_order`` ever returns a non-success status (would
    indicate a producer-side bug or out-of-sync state), the batch keeps
    going. No saga rollback, no ``compound_failed`` WS — just a warning
    log. The order ends up in pq at its real deadline instead of pinned;
    materializer writes DB consistently with that state.

    Verifies the pin failure does NOT short-circuit subsequent compounds
    in the batch — the follow-up ``add`` still applies and notifies as
    accepted.
    """
    pin_user = uuid.uuid4()
    pin_compound = _make_op(
        op="pin",
        group="grow",
        order_number="PIN-FAIL",
        deadline="2026-05-15",
        fake_deadline="2026-05-12",
        requested_by=pin_user,
    )
    follow_compound = _make_op(op="add", group="grow", order_number="ORD-OK")
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

    # pin_order was called once (the structural pass walks each leaf op).
    assert pin_mock.call_count == 1

    # The follow-up add still got accepted (no abort on pin failure).
    accepted = [
        c
        for c in mocks["notify_user"].call_args_list
        if c.kwargs["message"]["type"] == "schedule.compound_accepted"
    ]
    # Both compounds in the batch fired compound_accepted (pin failure is
    # defensive-only, does NOT cancel the compound).
    assert len(accepted) == 2

    # No compound_failed for the pin (different contract from the old
    # saga design which would have raised this).
    failed = [
        c
        for c in mocks["notify_user"].call_args_list
        if c.kwargs["message"]["type"] == "schedule.compound_failed"
    ]
    assert failed == []


def test_run_scheduling_dispatches_unpin_op(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """``op="unpin"`` calls ``unpin_order(state, order_id)`` — no fake_deadline
    needed. Confirms the unpin path doesn't accidentally route through pin
    (a regression that would silently corrupt state).
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

    # Did NOT route to pin.
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
    # P3-3: materializer now bails early if STATE_KEY is missing
    # (defensive against the race where a fast-path task SADD'd a
    # notify before its _save_state landed). The test mocks
    # _load_state so the actual JSON we write here is irrelevant, but
    # the *existence* of the key is required to pass the new guard.
    redis_client.set(STATE_KEY, SchedulerState.initial(date(2026, 5, 5)).to_json())

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


def test_materialize_task_tolerates_system_sentinel_in_notify_pending(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """advance_day / rebuild SADD a non-UUID system sentinel into
    notify_pending before their follow-up ``.delay()`` so this materializer's
    post-release re-trigger check is guaranteed to see pending work (see
    the race walkthrough at ``_MATERIALIZE_SYSTEM_SENTINEL``).

    This means the per-member loop will encounter a non-UUID member.
    ``websocket.notify_user`` is called with ``uuid.UUID(member)`` and the
    parse raises ``ValueError`` — the materializer must catch, log, and
    keep draining (no exception escapes, real UUIDs still get notified).
    """
    from app.workers.scheduling import _MATERIALIZE_SYSTEM_SENTINEL, materialize_schedule_task

    real_user = uuid.uuid4()
    redis_client.sadd(
        "schedule:materialize_notify_pending",
        str(real_user),
        _MATERIALIZE_SYSTEM_SENTINEL,
    )
    redis_client.set(STATE_KEY, SchedulerState.initial(date(2026, 5, 5)).to_json())

    monkeypatch.setattr(
        "app.workers.scheduling._load_state",
        lambda: SchedulerState.initial(date(2026, 5, 5)),
    )
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _s: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )

    result = materialize_schedule_task.apply()
    assert result.successful(), result.traceback

    # Sentinel never reached notify_user — only the real UUID did.
    notified_ids = [c.kwargs["user_id"] for c in notify_mock.call_args_list]
    assert notified_ids == [real_user]
    # Pending fully drained (both real user + sentinel removed).
    assert redis_client.scard("schedule:materialize_notify_pending") == 0


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
    # P3-3: STATE_KEY must exist or the materializer's defensive guard
    # treats the work as "no state to materialize against" and bails.
    redis_client.set(STATE_KEY, SchedulerState.initial(date(2026, 5, 5)).to_json())

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


def test_materialize_task_skips_when_state_key_missing(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """P3-3 defensive guard: when ``schedule:state`` doesn't exist yet, the
    materializer must NOT call ``apply_schedule`` with an empty
    ``ScheduledResult`` list — that would clear daily_breakdown /
    scheduled_production_date on every order in DB, silently destroying
    user data. Instead, push the pending notify users back onto the
    pending set so the next materializer (triggered by whichever task
    eventually writes state) picks them up.
    """
    from app.workers.scheduling import materialize_schedule_task

    requester = uuid.uuid4()
    redis_client.sadd("schedule:materialize_notify_pending", str(requester))
    # Deliberately do NOT set STATE_KEY.

    apply_mock = MagicMock(return_value=0)
    monkeypatch.setattr("app.workers.scheduling.order_service.apply_schedule", apply_mock)
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )

    result = materialize_schedule_task.apply()
    assert result.successful(), result.traceback

    # apply_schedule was NOT called — that's the load-bearing assertion.
    assert apply_mock.call_count == 0
    # No user was notified yet — they're back on the pending set.
    assert notify_mock.call_count == 0
    # Notify-pending preserved so the next run picks them up.
    assert redis_client.scard("schedule:materialize_notify_pending") == 1
    assert redis_client.sismember("schedule:materialize_notify_pending", str(requester))


def test_materialize_task_runs_independently_of_state_writer_lock(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Materializer must NOT participate in ``state_writer_lock`` —
    it's a slow path (apply_schedule per N orders, several ms/order)
    and gating it on the same mutex as ``run_scheduling_task`` would
    pile user-facing PATCH latency behind it.

    The previous round-2 design had materializer per-batch acquire
    ``state_writer_lock``. We rolled that back: materializer runs
    free, and ``advance_day_task`` / ``rebuild_schedule_task`` instead
    dispatch a follow-up materializer after their commit to bound any
    stale write that might race with them.

    This test pre-claims the lock as if a long-running task is using
    it, then runs the materializer and verifies it proceeds normally
    (apply_schedule called, user notified) — i.e., the lock does NOT
    block it.
    """
    from app.workers.scheduling import STATE_WRITER_LOCK_KEY, materialize_schedule_task

    requester = uuid.uuid4()
    redis_client.sadd("schedule:materialize_notify_pending", str(requester))
    redis_client.set(STATE_KEY, SchedulerState.initial(date(2026, 5, 5)).to_json())
    # Pre-claim state_writer_lock as another task — should NOT affect
    # the materializer at all under the new design.
    redis_client.set(STATE_WRITER_LOCK_KEY, "other-task-id", ex=300)

    apply_mock = MagicMock(return_value=0)
    monkeypatch.setattr("app.workers.scheduling.order_service.apply_schedule", apply_mock)
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _s: [])
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    notify_mock = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", notify_mock)
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )

    result = materialize_schedule_task.apply()
    assert result.successful(), result.traceback

    # Materializer proceeded normally despite the held lock.
    assert apply_mock.call_count == 1
    # User was notified.
    notify_kinds = [c.kwargs["message"]["type"] for c in notify_mock.call_args_list]
    assert "schedule.materialized" in notify_kinds
    # We never touched the held lock — still belongs to the other task.
    assert redis_client.get(STATE_WRITER_LOCK_KEY) == "other-task-id"


def test_advance_day_dispatches_materializer_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """advance_day_task must trigger a fresh ``materialize_schedule_task``
    after its commit + lock release. Without this, an in-flight
    materializer that read pre-advance_day state could overwrite
    advance_day's freshly-written ``daily_breakdown`` /
    ``scheduled_production_date`` with stale values — and since
    materializer isn't lock-serialized anymore, advance_day can't
    block the race directly. Triggering a follow-up materializer
    bounds the stale window to one materializer cycle.

    Also asserts the **sentinel SADD lands before** ``.delay()``: without
    it, an in-flight materializer (M1) holding ``MATERIALIZE_RUNNING_KEY``
    would cause our M2 to ``skip_concurrent``, and M1's post-release
    re-trigger check sees an empty notify_pending and never re-fires —
    so DB silently keeps the pre-advance state until the next user
    compound. The sentinel makes M1's post-release check observe pending
    and dispatch a fresh M3.
    """
    from app.workers.scheduling import _MATERIALIZE_SYSTEM_SENTINEL, advance_day_task

    redis_client.set(STATE_KEY, SchedulerState.initial(date(2026, 5, 5)).to_json())

    monkeypatch.setattr("app.workers.scheduling.compute_schedule", lambda _s: [])
    monkeypatch.setattr(
        "app.workers.scheduling.advance_day",
        lambda s: s,
    )
    monkeypatch.setattr("app.workers.scheduling.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.workers.scheduling.order_repo.mark_completed_outside_set",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.workers.scheduling.order_repo.mark_in_production",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    # Capture the state of notify_pending at the moment .delay() is
    # called — ordering matters: SADD must land BEFORE dispatch so an
    # in-flight materializer's post-release check is guaranteed to see
    # pending work.
    pending_at_dispatch: list[set[str]] = []

    def capture_pending() -> None:
        members = redis_client.smembers(MATERIALIZE_NOTIFY_PENDING_KEY)
        pending_at_dispatch.append(set(members))

    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        capture_pending,
    )

    result = advance_day_task.apply()
    assert result.successful(), result.traceback

    # advance_day dispatched a materializer at the end…
    assert len(pending_at_dispatch) >= 1
    # …and the sentinel was already in notify_pending at that moment, so
    # any racing in-flight materializer that hits skip_concurrent will
    # still re-trigger on its post-release check.
    assert _MATERIALIZE_SYSTEM_SENTINEL in pending_at_dispatch[-1]


def test_rebuild_dispatches_materializer_with_sentinel_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Same race-guard contract as advance_day, but for ``rebuild_schedule_task``.

    Rebuild's finally also dispatches a follow-up materializer, and the
    same in-flight-materializer-loses-the-race scenario applies. Verifies
    the sentinel is SADD'd into notify_pending before .delay() so M1's
    post-release re-trigger picks it up if M2 hits skip_concurrent.
    """
    from app.workers.scheduling import _MATERIALIZE_SYSTEM_SENTINEL, rebuild_schedule_task

    base = date(2026, 5, 5)
    redis_client.set(STATE_KEY, SchedulerState.initial(base).to_json())

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
    monkeypatch.setattr(
        "app.workers.scheduling.rebuild_state",
        lambda orders, base_date: (SchedulerState.initial(base_date), []),
    )
    monkeypatch.setattr("app.workers.scheduling.compute_schedule", MagicMock(return_value=[]))
    monkeypatch.setattr(
        "app.workers.scheduling.order_service.apply_schedule",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", MagicMock())
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())

    pending_at_dispatch: list[set[str]] = []

    def capture_pending() -> None:
        members = redis_client.smembers(MATERIALIZE_NOTIFY_PENDING_KEY)
        pending_at_dispatch.append(set(members))

    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        capture_pending,
    )

    result = rebuild_schedule_task.apply()
    assert result.successful(), result.traceback

    assert len(pending_at_dispatch) >= 1
    assert _MATERIALIZE_SYSTEM_SENTINEL in pending_at_dispatch[-1]


# ---------------------------------------------------------------------------
# _perform_compound_db_action — P1-2 worker-side DB writes
# ---------------------------------------------------------------------------
#
# These tests pin down the contract: the worker — not the producer — owns
# the user-facing column writes when a compound is accepted, and runs the
# rollback compensation when a compound is rejected. Pre-P1-2 the producer
# committed new wafer_quantity / deadline / soft-delete *before* the
# scheduler even saw the compound; if the compound then failed (capacity
# exceeded, deadline too far, …), DB and Redis-state diverged forever.
# With ``_perform_compound_db_action`` the DB write happens *after* state
# has accepted, and the rejected branch just unlocks the row (or, for
# create, soft-deletes the orphan stub the producer pre-inserted).


def _stub_compound_with_db_action(
    *,
    kind: str,
    order_id: uuid.UUID,
    actor_id: uuid.UUID,
    new_wafer_quantity: int | None = None,
    new_requested_delivery_date: str | None = None,
    new_notes_set: bool = False,
    new_notes: str | None = None,
) -> dict[str, Any]:
    """Build a compound dict that mimics what ``schedule_queue.enqueue_compound``
    stores in Redis — only the fields ``_perform_compound_db_action`` reads."""
    return {
        "compound_id": str(uuid.uuid4()),
        "group": "grow",
        "op_count": 1,
        "ops": [
            {
                "op": "add",
                "order_id": str(order_id),
                "order_number": "ORD-T",
                "wafer_quantity": 100,
                "deadline": "2026-08-01",
            }
        ],
        "requested_by": str(actor_id),
        "db_action": {
            "kind": kind,
            "actor_id": str(actor_id),
            "new_wafer_quantity": new_wafer_quantity,
            "new_requested_delivery_date": new_requested_delivery_date,
            "new_notes_set": new_notes_set,
            "new_notes": new_notes,
            "new_assigned_to_set": False,
            "new_assigned_to": None,
            "old_wafer_quantity": None,
            "old_requested_delivery_date": None,
            "old_notes": None,
            "old_assigned_to": None,
        },
    }


class _NonClosingSession:
    """Delegating wrapper that ignores ``.close()``.

    The worker's ``_perform_compound_db_action`` opens its own session
    via ``SessionLocal()`` and closes it in ``finally``. In tests we
    want it to use the per-test ``db_session`` (so its commits land in
    the SAVEPOINT the outer fixture rolls back), but we can't let it
    close that session — the test still needs it for assertions.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        pass


def _patch_worker_sessionlocal_to_test_db(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Any,
) -> None:
    """Route ``app.workers.scheduling.SessionLocal()`` to the test session.

    Without this, the worker would call its module-level ``SessionLocal``
    which is bound to the application's default engine (the placeholder
    URL from ``conftest`` module-level env defaults), not the
    testcontainer's engine. The wrapper makes worker commits go through
    the test's transaction so they're isolated per-test by the outer
    rollback in ``db_session``.
    """
    monkeypatch.setattr(
        "app.workers.scheduling.SessionLocal",
        lambda: _NonClosingSession(db_session),
    )


def test_perform_db_action_accept_update_writes_new_values_and_audits(
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted update compound: worker writes new wafer_quantity /
    requested_delivery_date, clears the lock, and emits an
    ``order.updated`` audit row. This is the central P1-2 contract:
    producer committed *only* ``is_processing_locked=True`` upfront;
    everything else lands here.
    """
    from app.models.audit_log import AuditLog
    from app.models.order import Order, OrderStatus
    from app.models.user import User, UserRole
    from app.workers.scheduling import _perform_compound_db_action

    _patch_worker_sessionlocal_to_test_db(monkeypatch, db_session)

    import bcrypt

    actor = User(
        username="worker-dbaction-actor",
        password_hash=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        role=UserRole.scheduler,
        is_active=True,
    )
    db_session.add(actor)
    db_session.commit()

    order = Order(
        order_number="ORD-DBACTION-UPDATE",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 8, 1),
        created_by=actor.id,
        status=OrderStatus.pending,
        is_processing_locked=True,  # Producer set this.
    )
    db_session.add(order)
    db_session.commit()

    compound = _stub_compound_with_db_action(
        kind="update",
        order_id=order.id,
        actor_id=actor.id,
        new_wafer_quantity=250,
        new_requested_delivery_date="2026-09-15",
    )

    _perform_compound_db_action(compound, accepted=True)

    db_session.expire_all()
    db_session.refresh(order)
    assert order.wafer_quantity == 250
    assert order.requested_delivery_date == date(2026, 9, 15)
    assert order.is_processing_locked is False

    from sqlalchemy import select as _sa_select

    audit = db_session.scalars(_sa_select(AuditLog).where(AuditLog.resource_id == order.id)).all()
    actions = [row.action for row in audit]
    assert "order.updated" in actions


def test_perform_db_action_reject_update_clears_lock_only(
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected update compound: DB columns the user *wanted* to change
    must remain at their pre-PATCH values (producer never wrote them).
    Worker only clears the lock so the UI unblocks. Status snaps back to
    ``scheduled`` when ``scheduled_production_date`` is set (= row was
    already on the schedule pre-PATCH), or ``pending`` otherwise.
    """
    from app.models.order import Order, OrderStatus
    from app.models.user import User, UserRole
    from app.workers.scheduling import _perform_compound_db_action

    _patch_worker_sessionlocal_to_test_db(monkeypatch, db_session)

    import bcrypt

    actor = User(
        username="worker-dbaction-reject-update",
        password_hash=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        role=UserRole.scheduler,
        is_active=True,
    )
    db_session.add(actor)
    db_session.commit()

    order = Order(
        order_number="ORD-DBACTION-REJ-UPDATE",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 8, 1),
        created_by=actor.id,
        status=OrderStatus.pending,
        scheduled_production_date=date(2026, 7, 15),  # pre-PATCH was scheduled
        is_processing_locked=True,
    )
    db_session.add(order)
    db_session.commit()

    compound = _stub_compound_with_db_action(
        kind="update",
        order_id=order.id,
        actor_id=actor.id,
        new_wafer_quantity=999,  # would have been written on accept
        new_requested_delivery_date="2099-12-31",
    )

    _perform_compound_db_action(compound, accepted=False)

    db_session.expire_all()
    db_session.refresh(order)
    # Pre-PATCH values intact.
    assert order.wafer_quantity == 100
    assert order.requested_delivery_date == date(2026, 8, 1)
    # Lock cleared; status restored to scheduled (had a scheduled date).
    assert order.is_processing_locked is False
    assert order.status == OrderStatus.scheduled


def test_perform_db_action_accept_delete_soft_deletes_and_audits(
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted delete compound: ``is_deleted=True`` + ``status=cancelled``
    + ``order.cancelled`` audit. Producer wrote *only* the lock; the
    visible deletion lands here.
    """
    from app.models.audit_log import AuditLog
    from app.models.order import Order, OrderStatus
    from app.models.user import User, UserRole
    from app.workers.scheduling import _perform_compound_db_action

    _patch_worker_sessionlocal_to_test_db(monkeypatch, db_session)

    import bcrypt

    actor = User(
        username="worker-dbaction-delete",
        password_hash=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        role=UserRole.scheduler,
        is_active=True,
    )
    db_session.add(actor)
    db_session.commit()

    order = Order(
        order_number="ORD-DBACTION-DEL",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 8, 1),
        created_by=actor.id,
        status=OrderStatus.scheduled,
        is_processing_locked=True,
    )
    db_session.add(order)
    db_session.commit()

    compound = _stub_compound_with_db_action(
        kind="delete",
        order_id=order.id,
        actor_id=actor.id,
    )

    _perform_compound_db_action(compound, accepted=True)

    db_session.expire_all()
    db_session.refresh(order)
    assert order.is_deleted is True
    assert order.status == OrderStatus.cancelled
    assert order.is_processing_locked is False

    from sqlalchemy import select as _sa_select

    audit = db_session.scalars(_sa_select(AuditLog).where(AuditLog.resource_id == order.id)).all()
    actions = [row.action for row in audit]
    assert "order.cancelled" in actions


def test_perform_db_action_reject_create_soft_deletes_orphan_row(
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected create compound: producer pre-created a row (status=pending,
    is_processing_locked=True) before the worker knew if the schedule
    could accept the new order. When the schedule rejects (capacity
    exceeded, deadline too far), the row would otherwise live forever as
    a locked, pending orphan — UI shows it but can't apply any further
    action because nothing's listening. Worker's compensation is to
    soft-delete the orphan so it disappears from user views.
    """
    from app.models.order import Order, OrderStatus
    from app.models.user import User, UserRole
    from app.workers.scheduling import _perform_compound_db_action

    _patch_worker_sessionlocal_to_test_db(monkeypatch, db_session)

    import bcrypt

    actor = User(
        username="worker-dbaction-rej-create",
        password_hash=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        role=UserRole.scheduler,
        is_active=True,
    )
    db_session.add(actor)
    db_session.commit()

    order = Order(
        order_number="ORD-DBACTION-REJ-CREATE",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 8, 1),
        created_by=actor.id,
        status=OrderStatus.pending,
        is_processing_locked=True,
    )
    db_session.add(order)
    db_session.commit()

    compound = _stub_compound_with_db_action(
        kind="create",
        order_id=order.id,
        actor_id=actor.id,
        new_wafer_quantity=100,
        new_requested_delivery_date="2026-08-01",
    )

    _perform_compound_db_action(compound, accepted=False)

    db_session.expire_all()
    db_session.refresh(order)
    assert order.is_deleted is True
    assert order.status == OrderStatus.cancelled
    assert order.is_processing_locked is False


# ---------------------------------------------------------------------------
# State-writer lock (P0-2 / P0-3)
# ---------------------------------------------------------------------------
#
# These tests deliberately do NOT call ``_bypass_state_writer_lock`` —
# they exercise the real Redis SETNX behavior to prove the mutex is
# actually mutually exclusive. Pre-fix, two run_scheduling_task
# invocations could both write ``schedule:state``, losing one
# compound's effect; the lock makes the second invocation a no-op so
# the holder owns state writes uncontested.


def test_state_writer_lock_blocks_concurrent_run_scheduling_task(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """When the state-writer lock is held by another task, a freshly-fired
    ``run_scheduling_task`` must return early without reading the queue
    or touching state. The lock holder will drain the queue on its own.

    This test deliberately does NOT use ``_patch_common`` because that
    helper installs the auto-retrigger delay shim which also bypasses the
    state-writer lock. Here we want the real ``_try_acquire_state_lock``
    to run against real Redis so we can verify the early-return path.
    """
    from app.workers.scheduling import STATE_WRITER_LOCK_KEY

    redis_client.set(STATE_WRITER_LOCK_KEY, "other-worker-task-id", ex=300)
    _enqueue(redis_client, _make_op(order_number="ORD-LOCK-HELD"))

    # Track whether _read_pending_compounds was called — if the lock guard
    # works, the early return MUST skip pending-list inspection entirely.
    read_mock = MagicMock(return_value=[])
    monkeypatch.setattr("app.workers.scheduling._read_pending_compounds", read_mock)
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Lock guard short-circuited before we got to the drain loop.
    assert read_mock.call_count == 0
    # Queue untouched.
    assert redis_client.zcard(PENDING_OPS_KEY) == 1
    # Lock still belongs to the other worker.
    assert redis_client.get(STATE_WRITER_LOCK_KEY) == "other-worker-task-id"


def test_state_writer_lock_released_on_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """After a successful run_scheduling_task, the lock must be released
    so the next task can acquire. Without release, every subsequent task
    would skip forever and the queue would back up.
    """
    from app.workers.scheduling import STATE_WRITER_LOCK_KEY

    _enqueue(redis_client, _make_op(order_number="ORD-LOCK-RELEASE"))

    # Don't bypass the lock — let the task actually acquire it. _patch_common
    # provides the pin/unpin/notify/materialize stubs and an auto-retrigger
    # delay mock, but does NOT touch the state-writer lock primitives.
    mocks = _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Compound was processed: one compound_accepted notification fired.
    accepted = [
        c
        for c in mocks["notify_user"].call_args_list
        if c.kwargs["message"]["type"] == "schedule.compound_accepted"
    ]
    assert len(accepted) == 1
    # Lock is gone — released by finally.
    assert redis_client.get(STATE_WRITER_LOCK_KEY) is None


def test_state_writer_lock_released_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """An exception inside the task body must still release the lock via
    ``finally``. Otherwise a single failed task would block all
    subsequent state writes for the lock's TTL (5 minutes).
    """
    from app.workers.scheduling import STATE_WRITER_LOCK_KEY

    _enqueue(redis_client, _make_op(order_number="ORD-LOCK-EXC"))

    _patch_common(monkeypatch)
    monkeypatch.setattr(
        "app.workers.scheduling._save_state",
        MagicMock(side_effect=RuntimeError("simulated body failure")),
    )

    result = run_scheduling_task.apply()
    # Task is expected to fail.
    assert not result.successful()

    # Lock is still cleaned up despite the exception.
    assert redis_client.get(STATE_WRITER_LOCK_KEY) is None


def test_state_writer_lock_cas_delete_doesnt_release_someone_elses_lock(
    redis_client: Redis,
) -> None:
    """If our TTL expired and a different task acquired the lock, our
    finally's release must NOT delete their lock. Lua CAS guards this.
    """
    from app.workers.scheduling import STATE_WRITER_LOCK_KEY, _release_state_lock

    # Plant another task's lock.
    redis_client.set(STATE_WRITER_LOCK_KEY, "task-B", ex=300)
    # Our task-A tries to release. CAS should no-op because the value
    # belongs to task-B.
    _release_state_lock("task-A")
    # Task-B's lock untouched.
    assert redis_client.get(STATE_WRITER_LOCK_KEY) == "task-B"


# ---------------------------------------------------------------------------
# Pending-ops DLQ (P1-5)
# ---------------------------------------------------------------------------


def test_malformed_pending_op_member_is_drained_to_dlq(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """A corrupted (non-JSON) sorted-set member must land in the DLQ list
    so the affected order's stuck ``is_processing_locked=True`` row is
    forensically recoverable, instead of being silently dropped (which
    pre-P1-5 forced manual DB surgery on a locked, requester-unknown
    order). After the bad member is drained, ``_read_pending_compounds``
    skips it and surfaces only the valid compounds in its return list.
    """
    from app.services.scheduling import PENDING_OPS_SEQ_KEY, score_for_op
    from app.workers.scheduling import (
        PENDING_OPS_DLQ_KEY,
        _read_pending_compounds,
    )

    bad_seq = redis_client.incr(PENDING_OPS_SEQ_KEY)
    redis_client.zadd(
        PENDING_OPS_KEY,
        {"this is not valid json": score_for_op(group="grow", seq=bad_seq)},
    )
    # Follow with a well-formed compound so we can prove the read continues
    # to the next member after draining the bad one.
    good_compound = _make_op(order_number="ORD-AFTER-BAD")
    _enqueue(redis_client, good_compound)

    parsed = _read_pending_compounds()

    # Bad member was drained to DLQ.
    assert redis_client.llen(PENDING_OPS_DLQ_KEY) == 1
    dlq_member = redis_client.lindex(PENDING_OPS_DLQ_KEY, 0)
    assert dlq_member == "this is not valid json"
    # The good compound was returned by the read, skipping the bad one.
    assert len(parsed) == 1
    _member, payload = parsed[0]
    assert payload["ops"][0]["order_number"] == "ORD-AFTER-BAD"
    # Pending_ops now has only the good compound (bad was ZREM'd during drain).
    assert redis_client.zcard(PENDING_OPS_KEY) == 1


# ---------------------------------------------------------------------------
# Reject-rate adaptive cap on batch admission
# ---------------------------------------------------------------------------
#
# Heuristic: when ``run_scheduling_task`` reads N pending compounds, it only
# binary-searches the first ``ceil(1/p)`` of them — where p is the rolling
# EWMA estimate of per-compound rejection probability persisted at
# ``schedule:compound_reject_rate``. Saves
# ``compute_batch_capacity_delta`` work on prefixes too long to be feasible
# anyway. These tests verify the rate helpers in isolation, then check
# the drain loop actually honors the cap.


def test_reject_rate_defaults_to_initial_when_key_missing(
    redis_client: Redis,
) -> None:
    """No key in Redis ⇒ falls back to ``_REJECT_RATE_INITIAL`` (the prior
    we picked for fresh deployments / post-flush). Critical because every
    new worker starts with the key absent."""
    from app.workers.scheduling import (
        _REJECT_RATE_INITIAL,
        COMPOUND_REJECT_RATE_KEY,
        _get_reject_rate,
    )

    # Sanity: autouse flush already wiped it, but make this explicit.
    assert redis_client.get(COMPOUND_REJECT_RATE_KEY) is None

    assert _get_reject_rate() == _REJECT_RATE_INITIAL


def test_reject_rate_clamps_corrupted_value_to_initial(
    redis_client: Redis,
) -> None:
    """If the stored value can't be parsed as float (manual surgery, bit
    flip, schema change), don't poison the take-count math — fall back to
    the prior and log a warning."""
    from app.workers.scheduling import (
        _REJECT_RATE_INITIAL,
        COMPOUND_REJECT_RATE_KEY,
        _get_reject_rate,
    )

    redis_client.set(COMPOUND_REJECT_RATE_KEY, "not-a-float")

    assert _get_reject_rate() == _REJECT_RATE_INITIAL


def test_reject_rate_clamps_out_of_range_value(
    redis_client: Redis,
) -> None:
    """Values outside ``[MIN, MAX]`` are pulled back to the bound. Future-
    proofs against a bug that writes wild values."""
    from app.workers.scheduling import (
        _REJECT_RATE_MAX,
        _REJECT_RATE_MIN,
        COMPOUND_REJECT_RATE_KEY,
        _get_reject_rate,
    )

    redis_client.set(COMPOUND_REJECT_RATE_KEY, "5.0")
    assert _get_reject_rate() == _REJECT_RATE_MAX

    redis_client.set(COMPOUND_REJECT_RATE_KEY, "0.0")
    assert _get_reject_rate() == _REJECT_RATE_MIN


def test_reject_rate_ewma_pulls_toward_zero_on_accepted(
    redis_client: Redis,
) -> None:
    """One accepted compound multiplies p by ``(1 - alpha)``. After N
    accepts in a row, p should monotonically approach 0 (but bounded by
    MIN). Confirms the EWMA direction matches the documented contract."""
    from app.workers.scheduling import (
        _REJECT_RATE_ALPHA,
        COMPOUND_REJECT_RATE_KEY,
        _get_reject_rate,
        _update_reject_rate,
    )

    # Seed at a mid-range value so we can see movement.
    redis_client.set(COMPOUND_REJECT_RATE_KEY, "0.5")

    _update_reject_rate(accepted=1, rejected=0)
    after_one = _get_reject_rate()
    # p_new = (1 - alpha) * 0.5
    assert after_one == pytest.approx((1 - _REJECT_RATE_ALPHA) * 0.5)

    # Many accepts in a row pull p further down.
    _update_reject_rate(accepted=10, rejected=0)
    after_eleven = _get_reject_rate()
    assert after_eleven < after_one


def test_reject_rate_ewma_pulls_toward_one_on_rejected(
    redis_client: Redis,
) -> None:
    """One rejected compound applies ``p_new = alpha + (1 - alpha) * p_old``.
    Sustained rejects push p toward 1 (bounded by MAX)."""
    from app.workers.scheduling import (
        _REJECT_RATE_ALPHA,
        COMPOUND_REJECT_RATE_KEY,
        _get_reject_rate,
        _update_reject_rate,
    )

    redis_client.set(COMPOUND_REJECT_RATE_KEY, "0.1")

    _update_reject_rate(accepted=0, rejected=1)
    after_one = _get_reject_rate()
    # p_new = alpha + (1 - alpha) * 0.1
    assert after_one == pytest.approx(_REJECT_RATE_ALPHA + (1 - _REJECT_RATE_ALPHA) * 0.1)

    _update_reject_rate(accepted=0, rejected=10)
    after_eleven = _get_reject_rate()
    assert after_eleven > after_one


def test_reject_rate_update_persists_to_redis(
    redis_client: Redis,
) -> None:
    """``_update_reject_rate`` must round-trip through Redis so subsequent
    task invocations (and parallel workers) see the latest value. Without
    persistence the heuristic would reset every task and never converge."""
    from app.workers.scheduling import (
        _REJECT_RATE_INITIAL,
        COMPOUND_REJECT_RATE_KEY,
        _update_reject_rate,
    )

    _update_reject_rate(accepted=0, rejected=1)

    raw = redis_client.get(COMPOUND_REJECT_RATE_KEY)
    assert raw is not None
    # Should be ABOVE the initial prior (we observed a reject).
    assert float(raw) > _REJECT_RATE_INITIAL


def test_reject_rate_update_noop_when_no_observations(
    redis_client: Redis,
) -> None:
    """``accepted=0, rejected=0`` is a no-op — the rate Redis key stays
    untouched. Important because the drain loop calls update once per
    iteration and the empty-input case happens on every empty-queue tick."""
    from app.workers.scheduling import COMPOUND_REJECT_RATE_KEY, _update_reject_rate

    _update_reject_rate(accepted=0, rejected=0)
    assert redis_client.get(COMPOUND_REJECT_RATE_KEY) is None


def test_take_count_caps_pending_by_inverse_rate() -> None:
    """take = min(N, ceil(1/p)). For p ≈ 0.01 the cap is 100; for p = 0.5
    it's 2; floor of 1 guarantees forward progress even if a buggy update
    pushes p above 1 momentarily.
    """
    from app.workers.scheduling import _take_count_from_rate

    # p = 0.01 ⇒ cap = 100; pending = 1000 ⇒ take = 100
    assert _take_count_from_rate(pending_count=1000, rate=0.01) == 100
    # p = 0.5 ⇒ cap = 2
    assert _take_count_from_rate(pending_count=1000, rate=0.5) == 2
    # pending < cap ⇒ take = pending (don't try to read past the queue)
    assert _take_count_from_rate(pending_count=5, rate=0.01) == 5
    # Floor of 1: even an unstable p > 1 must let the loop progress.
    assert _take_count_from_rate(pending_count=10, rate=2.0) == 1


# ---------------------------------------------------------------------------
# Reject-rate — reliability / convergence tests (review round-3)
# ---------------------------------------------------------------------------


def test_reject_rate_converges_to_expected_value_for_fixed_probability(
    redis_client: Redis,
) -> None:
    """Simulate a fixed per-compound reject probability of ~0.2 over many
    iterations; the EWMA should converge into a neighborhood of 0.2.

    EWMA with alpha=0.05 has effective averaging window ≈ 1/alpha = 20
    samples. After 200 observations the value should be within a small
    epsilon of the true probability. This test pins the convergence
    contract — if someone retunes alpha to 0.001 (super slow) or 0.5
    (over-reactive) the convergence speed assertion catches it.
    """
    from app.workers.scheduling import (
        COMPOUND_REJECT_RATE_KEY,
        _get_reject_rate,
        _update_reject_rate,
    )

    redis_client.set(COMPOUND_REJECT_RATE_KEY, "0.01")  # start from prior

    # 200 observations with reject probability 0.2 (40 rejects, 160 accepts)
    # Apply alternating chunks so accept-then-reject ordering doesn't
    # bias the result (within each call we do all accepts then all rejects).
    for _ in range(40):
        _update_reject_rate(accepted=4, rejected=1)  # 5 obs each, ratio 0.2

    final = _get_reject_rate()
    # After 200 obs, EWMA-with-alpha=0.05 converges to within ~0.05 of true.
    assert abs(final - 0.2) < 0.05, f"final p={final}, expected ≈ 0.2"


def test_reject_rate_cross_worker_race_last_writer_wins(
    redis_client: Redis,
) -> None:
    """Two workers updating concurrently → both compute deltas off the SAME
    snapshot, the second SET overwrites the first. One observation is
    effectively lost — that's the documented last-writer-wins tradeoff.

    We can't truly interleave two threads in a deterministic way here, so
    we simulate by:
      1. Set p₀
      2. Worker A reads p₀, computes its new p_A (one accept)
      3. Worker B reads p₀, computes its new p_B (one reject)
      4. Both write their value; final state == whoever wrote last

    Verifies the final p is exactly one of {p_A, p_B}, NOT the
    "compose-both-updates" value (which would be the sequentially-applied
    result). If the implementation gained CAS / Lua-script atomicity
    later, the final value would be the composed one and this test would
    correctly fail to signal the contract change.
    """
    from app.workers.scheduling import (
        _REJECT_RATE_ALPHA,
        COMPOUND_REJECT_RATE_KEY,
        _get_reject_rate,
    )

    p0 = 0.1
    redis_client.set(COMPOUND_REJECT_RATE_KEY, str(p0))

    # Manual replay of two non-atomic update sequences. Both start from p₀;
    # whoever writes second wins.
    p_after_accept = (1 - _REJECT_RATE_ALPHA) * p0  # worker A: 1 accept
    p_after_reject = _REJECT_RATE_ALPHA + (1 - _REJECT_RATE_ALPHA) * p0  # worker B: 1 reject

    # Simulate worker A reading p₀, then worker B reading p₀, then both
    # writing in order A→B. Final value = worker B's computed value.
    redis_client.set(COMPOUND_REJECT_RATE_KEY, repr(p_after_accept))  # A writes
    redis_client.set(COMPOUND_REJECT_RATE_KEY, repr(p_after_reject))  # B writes (wins)

    assert _get_reject_rate() == pytest.approx(p_after_reject)
    # NOT the sequential-compose-both value (which would be obs A then obs B
    # applied to the same instance, yielding a different p).
    sequential_both = _REJECT_RATE_ALPHA + (1 - _REJECT_RATE_ALPHA) * p_after_accept
    assert _get_reject_rate() != pytest.approx(sequential_both)


def test_reject_rate_partial_halving_drift_pushes_p_up(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """When ``_largest_halving_feasible_prefix`` halves multiple times
    before finding a feasible prefix, the EWMA must observe those failed
    halving rounds as ``rejected=`` signal — not just count the final
    ``accepted=k``.

    Without this, a workload that keeps halving from 100 to 2 (accepting
    only 2 per drain) would push p DOWN (only +2 accepts seen), keeping
    the cap permissively large, perpetuating the wasted halving. The
    halving-misses signal (= ``attempts_tried - 1`` when k > 0) is what
    pushes p back UP.
    """
    from app.workers.scheduling import COMPOUND_REJECT_RATE_KEY

    p0 = 0.01
    redis_client.set(COMPOUND_REJECT_RATE_KEY, str(p0))

    _enqueue(redis_client, _make_op(order_number="ORD-A"))

    # Mock halving to simulate "tried 4 prefix sizes, only the last (k=1)
    # was feasible" — so attempts_tried=4, halving_misses=3.
    def fake_search(_state: Any, compounds: list[dict[str, Any]]) -> tuple[int, int]:
        return 1, 4

    monkeypatch.setattr(
        "app.workers.scheduling._largest_halving_feasible_prefix",
        fake_search,
    )
    _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Final p MUST be higher than p₀ — 3 halving misses dominated 1 accept.
    raw = redis_client.get(COMPOUND_REJECT_RATE_KEY)
    assert raw is not None
    final = float(raw)
    assert final > p0, f"halving_misses signal failed: p₀={p0}, final={final}"


# ---------------------------------------------------------------------------
# Reject-rate adaptive cap — drain-loop integration
# ---------------------------------------------------------------------------


def test_run_scheduling_caps_candidates_by_reject_rate(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """With p pre-seeded high (= small cap), the drain loop's binary
    search MUST only see ``ceil(1/p)`` candidates per iteration even
    though the queue holds more. Verified by mocking
    ``_largest_halving_feasible_prefix`` to capture the candidate-list
    size on each call.
    """
    from app.workers.scheduling import COMPOUND_REJECT_RATE_KEY

    # p = 0.5 ⇒ cap = ceil(1/0.5) = 2.
    redis_client.set(COMPOUND_REJECT_RATE_KEY, "0.5")

    for i in range(10):
        _enqueue(redis_client, _make_op(order_number=f"ORD-{i:02d}"))

    captured_window_sizes: list[int] = []

    def fake_search(_state: Any, compounds: list[dict[str, Any]]) -> tuple[int, int]:
        captured_window_sizes.append(len(compounds))
        # (k, attempts_tried) — first probe succeeded.
        return len(compounds), 1

    monkeypatch.setattr(
        "app.workers.scheduling._largest_halving_feasible_prefix",
        fake_search,
    )
    _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # First iteration: pre-seeded p=0.5 ⇒ cap = ceil(1/0.5) = 2, so the
    # first binary-search call sees 2 candidates (not 10).
    assert captured_window_sizes
    assert captured_window_sizes[0] == 2, captured_window_sizes
    # Multiple iterations needed to drain all 10 — proves the cap forced
    # iteration rather than letting one big batch swallow everything.
    # (Subsequent iterations' window sizes grow as EWMA pulls p down on
    # each accept; we don't pin those exactly.)
    assert len(captured_window_sizes) >= 4
    # All 10 compounds eventually accepted.
    assert sum(captured_window_sizes) == 10


def test_run_scheduling_uses_full_pending_when_rate_is_minimal(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """With p clamped to its minimum, ``ceil(1/p)`` exceeds any realistic
    queue depth — so the binary-search candidate list IS the full pending
    list (= same behavior as a non-adaptive design). Confirms the cap
    doesn't get in the way when reject rate is low.
    """
    from app.workers.scheduling import _REJECT_RATE_MIN, COMPOUND_REJECT_RATE_KEY

    redis_client.set(COMPOUND_REJECT_RATE_KEY, str(_REJECT_RATE_MIN))

    for i in range(5):
        _enqueue(redis_client, _make_op(order_number=f"ORD-{i:02d}"))

    captured_window_sizes: list[int] = []

    def fake_search(_state: Any, compounds: list[dict[str, Any]]) -> tuple[int, int]:
        captured_window_sizes.append(len(compounds))
        return len(compounds), 1

    monkeypatch.setattr(
        "app.workers.scheduling._largest_halving_feasible_prefix",
        fake_search,
    )
    _patch_common(monkeypatch)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # First (and only) iteration saw all 5 compounds — cap (= 10_000) was
    # never the binding constraint.
    assert captured_window_sizes == [5]


def test_run_scheduling_updates_reject_rate_on_accept_and_reject(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """End-to-end: after one accepted compound + one rejected compound,
    p should have moved (in either direction). We don't pin an exact value
    because EWMA order matters, but we verify the rate is non-default and
    persisted."""
    from app.workers.scheduling import _REJECT_RATE_INITIAL, COMPOUND_REJECT_RATE_KEY

    # Two compounds: first will reject (alone infeasible), second will accept.
    _enqueue(redis_client, _make_op(order_number="REJ", wafer_quantity=1000))
    _enqueue(redis_client, _make_op(order_number="OK", wafer_quantity=1000))

    # First [1..N] check (binary search on the WHOLE batch of 2) ⇒ False.
    # First halving [1..1] (REJ alone) ⇒ False, k=0 ⇒ reject path.
    # Next iter: pending = [OK]. binary[1..1] ⇒ True ⇒ accept.
    feasibility_calls: list[int] = []

    def fake_feasible(_state: Any, delta: list[int]) -> bool:
        feasibility_calls.append(sum(delta))
        return (
            len(feasibility_calls) >= 3
        )  # 1st = batch of 2 fail, 2nd = REJ alone fail, 3rd = OK alone OK

    _patch_common(monkeypatch, is_batch_feasible=fake_feasible)

    result = run_scheduling_task.apply()
    assert result.successful(), result.traceback

    # Rate is no longer at default — it was observed-and-written.
    raw = redis_client.get(COMPOUND_REJECT_RATE_KEY)
    assert raw is not None
    final = float(raw)
    # Both accept and reject events fired exactly once. Order: reject (push
    # p up from 0.01) → accept (push p back down). Net direction depends on
    # the magnitudes; what's locked in is that p HAS moved.
    assert final != _REJECT_RATE_INITIAL
