"""End-to-end timing benchmark for the two write paths.

Each measurement runs 1 warmup + 3 timed trials and reports
``min / median / max`` in milliseconds. Single trials had unpleasant
~4x noise (GC pauses, OS scheduler hiccups, postgres autovacuum etc.);
median absorbs that.

Measures the wall-clock cost of:

1. ``run_scheduling_task`` — the **fast path** (state mutation + Redis
   ``_save_state`` + per-compound DB row write via ``_perform_compound_db_action``
   + WebSocket ``compound_accepted``).
2. ``materialize_schedule_task`` — the **slow path** (``compute_schedule``
   + ``apply_schedule`` which does ``clear_scheduled_dates`` plus per-order
   ``set_schedule_dates`` plus per-order audit-log writes, then WebSocket
   ``schedule.materialized``).

Both run against the live testcontainer DB + Redis so the numbers reflect
real I/O cost — not in-memory mock time. The benchmark seeds N orders in
DB, builds a corresponding ``SchedulerState`` in Redis, enqueues one
compound, and times each task with ``time.perf_counter()``.

This is **not** a regression-gated test — it always passes; the value is
in the printed report. Run with ``-s`` to see the timing table:

    uv run python -m pytest tests/workers/test_scheduling_benchmark.py -s

Numbers will vary by host (CPU, disk, container overhead). Use them to
reason about relative cost (per-order linear in materializer? state save
constant?) and to spot regressions, not as an absolute SLO.
"""

from __future__ import annotations

import statistics
import time
import uuid
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock

import bcrypt
import pytest
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from app.services.scheduling import (
    PENDING_OPS_KEY,
    PENDING_OPS_SEQ_KEY,
    STATE_KEY,
    SchedulerState,
    SchedulingOrder,
    add_order,
    score_for_op,
)
from redis import Redis
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Tunable benchmark sizes
# ---------------------------------------------------------------------------
#
# Per-N expected behaviour (no SLO, just intuition):
#   - run_scheduling_task: ~O(log n) state mutate + O(n) Redis serialize +
#     O(1) DB write per compound. Should be flat-ish vs N.
#   - materialize_schedule_task: O(n) compute_schedule + O(n) DB writes
#     (one UPDATE per order + one audit_log INSERT per order). Should
#     scale roughly linearly with N.

_BENCH_SIZES = [10, 100, 500]

# Number of timed trials per N. Each test does 1 warmup + this many
# measured runs; we report min/median/max so a single GC pause or
# autovacuum doesn't make a number look weird.
_TRIALS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_db_orders(
    db_session: Session,
    *,
    creator_id: uuid.UUID,
    n: int,
    base: date,
) -> list[Order]:
    """Insert N orders with status=scheduled + the columns apply_schedule
    will rewrite (scheduled_production_date / expected_delivery_date /
    daily_breakdown). Mirrors what an in-flight production looks like.
    """
    orders = [
        Order(
            order_number=f"BENCH-{i:05d}-{uuid.uuid4().hex[:6]}",
            customer_name="BenchCo",
            wafer_quantity=100,
            requested_delivery_date=base + timedelta(days=(i % 25) + 2),
            scheduled_production_date=base + timedelta(days=(i % 25) + 2),
            expected_delivery_date=base + timedelta(days=(i % 25) + 2),
            status=OrderStatus.scheduled,
            created_by=creator_id,
        )
        for i in range(n)
    ]
    db_session.add_all(orders)
    db_session.commit()
    for o in orders:
        db_session.refresh(o)
    return orders


def _seed_redis_state(
    redis_client: Redis,
    *,
    orders: list[Order],
    base: date,
) -> SchedulerState:
    """Build an in-memory ``SchedulerState`` matching the DB orders and
    save to Redis at ``STATE_KEY``. Returns the state for reference.
    """
    state = SchedulerState.initial(base)
    for o in orders:
        add_order(
            state,
            SchedulingOrder(
                order_id=o.id,
                order_number=o.order_number,
                wafer_quantity=o.wafer_quantity,
                deadline=o.requested_delivery_date,
            ),
        )
    redis_client.set(STATE_KEY, state.to_json())
    return state


def _enqueue_benchmark_compound(
    redis_client: Redis,
    *,
    target_order: Order,
    actor_id: uuid.UUID,
) -> dict[str, Any]:
    """Push a single PATCH-style compound (``[remove(old), add(new)]``)
    that will exercise the full fast-path code path: pop, apply ops,
    save state, perform db_action.
    """
    compound_id = str(uuid.uuid4())
    seq = int(redis_client.incr(PENDING_OPS_SEQ_KEY))
    new_qty = target_order.wafer_quantity + 50  # within CHECK constraint
    payload: dict[str, Any] = {
        "compound_id": compound_id,
        "group": "grow",
        "op_count": 2,
        "ops": [
            {
                "op": "remove",
                "order_id": str(target_order.id),
                "order_number": target_order.order_number,
                "wafer_quantity": target_order.wafer_quantity,
                "deadline": target_order.requested_delivery_date.isoformat(),
            },
            {
                "op": "add",
                "order_id": str(target_order.id),
                "order_number": target_order.order_number,
                "wafer_quantity": new_qty,
                "deadline": target_order.requested_delivery_date.isoformat(),
            },
        ],
        "requested_by": str(actor_id),
        "db_action": {
            "kind": "update",
            "actor_id": str(actor_id),
            "new_wafer_quantity": new_qty,
            "new_requested_delivery_date": target_order.requested_delivery_date.isoformat(),
            "new_notes_set": False,
            "new_notes": None,
            "new_assigned_to_set": False,
            "new_assigned_to": None,
            "old_wafer_quantity": target_order.wafer_quantity,
            "old_requested_delivery_date": target_order.requested_delivery_date.isoformat(),
            "old_notes": None,
            "old_assigned_to": None,
        },
        "_seq": seq,
    }
    import json as _json

    member = _json.dumps(payload)
    score = score_for_op(group="grow", seq=seq)
    redis_client.zadd(PENDING_OPS_KEY, {member: score})
    return payload


def _patch_worker_io(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    """Route worker SessionLocal to the test transaction, mock the
    things we can't realistically time in-process (WebSocket Redis
    pub/sub, Celery .delay)."""
    from tests.workers.test_scheduling_task import (
        _bypass_state_writer_lock,
        _NonClosingSession,
    )

    monkeypatch.setattr(
        "app.workers.scheduling.SessionLocal",
        lambda: _NonClosingSession(db_session),
    )
    monkeypatch.setattr("app.workers.scheduling.websocket.broadcast", MagicMock())
    monkeypatch.setattr("app.workers.scheduling.websocket.notify_user", MagicMock())
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task.delay", MagicMock())
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )
    monkeypatch.setattr("app.workers.scheduling.enqueue_notify_user", lambda _u: None)
    # Bypass state_writer_lock so the recursive auto-retrigger doesn't
    # deadlock against itself (production runs the retrigger in a
    # separate Celery worker after lock release; tests don't).
    _bypass_state_writer_lock(monkeypatch)


def _make_actor(db_session: Session, username: str) -> User:
    actor = User(
        username=username,
        password_hash=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        role=UserRole.scheduler,
        is_active=True,
    )
    db_session.add(actor)
    db_session.commit()
    db_session.refresh(actor)
    return actor


# ---------------------------------------------------------------------------
# Benchmark body
# ---------------------------------------------------------------------------


def _summary(label: str, n: int, samples_ms: list[float]) -> str:
    lo = min(samples_ms)
    med = statistics.median(samples_ms)
    hi = max(samples_ms)
    return (
        f"[bench] {label:<22}  N={n:>4}  →  "
        f"min={lo:7.2f}  median={med:7.2f}  max={hi:7.2f} ms  (n_trials={len(samples_ms)})"
    )


@pytest.mark.parametrize("n_orders", _BENCH_SIZES)
def test_benchmark_run_scheduling_task(
    n_orders: int,
    db_session: Session,
    redis_client: Redis,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Time the fast path against N pre-existing orders. The compound
    targets one of them; the rest just contribute to state size /
    serialization cost.

    Warmup invocation absorbs import + connection-pool init cost so the
    measured runs reflect steady-state behavior. Median of N trials
    smooths out GC pauses / OS scheduler jitter.
    """
    from app.workers.scheduling import run_scheduling_task

    base = date(2026, 5, 12)
    actor = _make_actor(db_session, username=f"bench-run-{n_orders}-{uuid.uuid4().hex[:6]}")
    orders = _seed_db_orders(db_session, creator_id=actor.id, n=n_orders, base=base)
    _seed_redis_state(redis_client, orders=orders, base=base)
    _patch_worker_io(monkeypatch, db_session)

    # 1 warmup + ``_TRIALS`` measured trials. Each trial re-seeds state
    # (since the previous compound mutated it) and re-enqueues — so
    # every measured invocation does the same work.
    samples: list[float] = []
    for trial in range(_TRIALS + 1):
        _seed_redis_state(redis_client, orders=orders, base=base)
        _enqueue_benchmark_compound(redis_client, target_order=orders[0], actor_id=actor.id)
        start = time.perf_counter()
        result = run_scheduling_task.apply()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        assert result.successful(), result.traceback
        if trial > 0:  # discard warmup
            samples.append(elapsed_ms)

    with capsys.disabled():
        print("\n" + _summary("run_scheduling_task", n_orders, samples))


@pytest.mark.parametrize("n_orders", _BENCH_SIZES)
def test_benchmark_materialize_schedule_task(
    n_orders: int,
    db_session: Session,
    redis_client: Redis,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Time the slow path: compute_schedule + apply_schedule's per-order
    DB writes + audit log inserts."""
    from app.workers.scheduling import materialize_schedule_task

    base = date(2026, 5, 12)
    actor = _make_actor(db_session, username=f"bench-mat-{n_orders}-{uuid.uuid4().hex[:6]}")
    orders = _seed_db_orders(db_session, creator_id=actor.id, n=n_orders, base=base)
    _seed_redis_state(redis_client, orders=orders, base=base)
    _patch_worker_io(monkeypatch, db_session)

    # 1 warmup + ``_TRIALS`` measured trials. Each trial re-adds the
    # actor to notify_pending so the materializer has work to do; the
    # body of materialize_schedule_task drains it.
    samples: list[float] = []
    for trial in range(_TRIALS + 1):
        redis_client.sadd("schedule:materialize_notify_pending", str(actor.id))
        start = time.perf_counter()
        result = materialize_schedule_task.apply()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        assert result.successful(), result.traceback
        if trial > 0:
            samples.append(elapsed_ms)

    with capsys.disabled():
        print("\n" + _summary("materialize_schedule", n_orders, samples))
