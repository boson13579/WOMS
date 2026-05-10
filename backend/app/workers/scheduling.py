"""Celery scheduling tasks.

Wraps the pure scheduler in :mod:`app.services.scheduling` with the side-effects
needed for production:

- Persists ``SchedulerState`` to Redis between runs (key ``schedule:state``).
- Drains a Redis **sorted set** of pending order ops (``schedule:pending_ops``;
  producers ``ZADD`` with a score that encodes ``shrink-before-grow`` plus
  FIFO-within-group, and we ``ZPOPMIN`` — both are O(log n)). The score is
  built from a monotonic ``INCR`` counter at ``schedule:pending_ops:seq``.
- Tracks task lifecycle in ``schedule:status`` for dashboards / advance_day.
- Writes computed schedule rows back into Postgres.
- Notifies clients via the ``app.services.websocket`` placeholder.

Three tasks live here:

- ``run_scheduling_task`` — drain pending ops, mutate state, persist, notify,
  and re-fire itself if more ops queued during the run.
- ``advance_day_task`` — daily 00:00 UTC tick; waits for any in-flight run,
  rolls the horizon forward by a day, then re-triggers scheduling.
- ``rebuild_schedule_task`` — fired by ``POST /schedule/rebuild``; waits for
  any in-flight run, rebuilds state from DB, notifies skipped orders'
  creators, then re-triggers scheduling.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any, cast

import structlog
from celery import Task
from redis import Redis
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services import order as order_service
from app.services import websocket
from app.services.scheduling import (
    SchedulerState,
    SchedulingOrder,
    add_order,
    advance_day,
    compute_schedule,
    rebuild_state,
    remove_order,
)
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

__all__ = ["advance_day_task", "rebuild_schedule_task", "run_scheduling_task"]


# ---------------------------------------------------------------------------
# Redis key + status constants
# ---------------------------------------------------------------------------

STATE_KEY = "schedule:state"
PENDING_OPS_KEY = "schedule:pending_ops"
PENDING_OPS_SEQ_KEY = "schedule:pending_ops:seq"
STATUS_KEY = "schedule:status"
WAITER_FLAG_KEY = "schedule:waiter_pending"

_STATUS_IDLE = "idle"
_STATUS_RUNNING = "running"
_STATUS_FAILED = "failed"

# Crash-safety TTL for the waiter flag. If a waiter (advance_day_task /
# rebuild_schedule_task) sets the flag and then dies before its finally
# clears it, this TTL ensures future run_scheduling_task invocations stop
# yielding to a phantom waiter. 10 minutes is well above the worst-case
# wall time of any waiter body (5 min wait + work) but short enough that a
# real crash recovers within an operator-visible timeframe. Tunable via
# ``SCHEDULER_WAITER_FLAG_TTL_SECONDS`` env var.
_WAITER_FLAG_TTL_SECONDS = get_settings().SCHEDULER_WAITER_FLAG_TTL_SECONDS

# Score layout for ``schedule:pending_ops`` (sorted set):
#   score = GROUP_OFFSET * group_priority + seq
# where group_priority is 0 for shrink-group ops (popped first) and 1 for
# grow-group ops, and ``seq`` is a monotonically increasing INCR counter
# (oldest op = smallest seq = popped first within its group).
#
# 10**12 is large enough that we'd need a trillion ops in the shrink group
# before colliding with the grow group's score range, while still well
# within float64's exact-integer range (2**53 ~= 9.0e15) so ZPOPMIN
# ordering is stable.
GROUP_OFFSET = 10**12

# advance_day_task and rebuild_schedule_task both wait at most this long for
# an in-flight run to finish before proceeding. Polling cadence is short
# enough to be responsive and long enough to keep Redis traffic negligible.
# Tunable via ``SCHEDULER_RUN_WAIT_TIMEOUT_SECONDS`` /
# ``SCHEDULER_RUN_WAIT_POLL_INTERVAL_SECONDS`` env vars.
_RUN_WAIT_TIMEOUT_SECONDS = get_settings().SCHEDULER_RUN_WAIT_TIMEOUT_SECONDS
_RUN_WAIT_POLL_INTERVAL_SECONDS = get_settings().SCHEDULER_RUN_WAIT_POLL_INTERVAL_SECONDS


def score_for_op(*, group: str, seq: int) -> float:
    """Compute the ZADD score for a pending op.

    Producers (the API endpoint) use this to ensure the same scoring scheme
    the worker assumes when it ZPOPMIN's. Exposed at module level so callers
    don't have to know the encoding.
    """
    if group not in ("shrink", "grow"):
        raise ValueError(f"unknown pending-op group: {group!r}")
    return float((0 if group == "shrink" else 1) * GROUP_OFFSET + seq)


# ---------------------------------------------------------------------------
# Lazy Redis client
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_redis() -> Redis:
    """Module-level Redis client; instantiated on first use."""
    return Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True)


# ---------------------------------------------------------------------------
# State / status / pending_ops accessors
# ---------------------------------------------------------------------------


def _load_state() -> SchedulerState:
    """Read ``schedule:state`` or initialize a fresh one anchored at today."""
    raw = cast("str | None", _get_redis().get(STATE_KEY))
    if raw is None:
        return SchedulerState.initial(datetime.now(tz=UTC).date())
    return SchedulerState.from_json(raw)


def _save_state(state: SchedulerState) -> None:
    """Serialize and persist the scheduler state to Redis."""
    _get_redis().set(STATE_KEY, state.to_json())


def _set_status(
    *,
    state: str,
    started_at: str | None = None,
    finished_at: str | None = None,
    task_id: str | None = None,
    error: str | None = None,
) -> None:
    """Overwrite ``schedule:status`` with a single JSON document."""
    payload = {
        "state": state,
        "started_at": started_at,
        "finished_at": finished_at,
        "task_id": task_id,
        "error": error,
    }
    _get_redis().set(STATUS_KEY, json.dumps(payload))


def _get_status() -> dict[str, Any] | None:
    raw = cast("str | None", _get_redis().get(STATUS_KEY))
    if raw is None:
        return None
    return cast("dict[str, Any]", json.loads(raw))


def _set_waiter_flag() -> None:
    """Mark that a waiter task is in the pipeline.

    A ``run_scheduling_task`` that finishes while this flag is set must NOT
    re-trigger itself even if more ops are queued — the waiter
    (``advance_day_task`` / ``rebuild_schedule_task``) takes ownership of
    the next ``run_scheduling_task.delay()`` after its own work finishes.
    Without this flag the re-triggered run_task and the waiter race each
    other writing ``schedule:state``: the waiter sees ``status=idle`` the
    instant the run_task flips it, but run_task hasn't yet fired its
    re-trigger, and ends up doing so a few microseconds later → both run
    concurrently.

    A 10-minute TTL guards against a crashed waiter permanently
    suppressing re-triggers; after expiry the system is back to the
    "no waiter" baseline.
    """
    _get_redis().set(WAITER_FLAG_KEY, "1", ex=_WAITER_FLAG_TTL_SECONDS)


def _clear_waiter_flag() -> None:
    """Drop the waiter flag.

    Called from a ``finally`` so it always runs, even when the waiter body
    raised.
    """
    _get_redis().delete(WAITER_FLAG_KEY)


def _is_waiter_pending() -> bool:
    """True iff a waiter has set the flag and not yet cleared it."""
    return _get_redis().get(WAITER_FLAG_KEY) is not None


def _pop_next_op() -> dict[str, Any] | None:
    """Pop the highest-priority pending op, or ``None`` if the queue is empty.

    Priority: shrink-group ops before grow-group ops; FIFO within each group.
    Both invariants are encoded in the sorted-set score by the producer
    (``score_for_op``), so this function is just an atomic ``ZPOPMIN`` —
    O(log n) regardless of queue depth, and no scan-and-sort like the old
    list-based implementation.

    Calling this in a loop — re-popping after every processed op — is what
    lets a freshly-arrived shrink op interrupt the tail of an old grow batch:
    its score (``< GROUP_OFFSET``) is smaller than every pending grow op's
    score (``>= GROUP_OFFSET``), so the next ``ZPOPMIN`` picks it up before
    any remaining grow.

    Malformed JSON entries (shouldn't happen given the schema-validated
    producer, but Redis itself doesn't enforce content) are silently
    discarded with a warning; the loop continues until it finds a valid
    op or empties the queue.
    """
    rds = _get_redis()
    while True:
        result = cast("list[tuple[str, float]]", rds.zpopmin(PENDING_OPS_KEY, 1))
        if not result:
            return None
        member, _score = result[0]
        try:
            return cast("dict[str, Any]", json.loads(member))
        except json.JSONDecodeError:
            logger.warning("schedule.pending_op.malformed", raw=member)
            # Discarded by ZPOPMIN already; loop to try the next one.


def _op_to_scheduling_order(op: dict[str, Any]) -> SchedulingOrder:
    """Translate a queued op dict into the pure scheduler's input schema."""
    return SchedulingOrder(
        order_id=uuid.UUID(op["order_id"]),
        order_number=op["order_number"],
        wafer_quantity=int(op["wafer_quantity"]),
        deadline=date.fromisoformat(op["deadline"]),
    )


# ---------------------------------------------------------------------------
# Pending-op application
# ---------------------------------------------------------------------------


def _process_one(state: SchedulerState, op: dict[str, Any]) -> None:
    """Run a single ``add`` or ``remove`` against *state* with logging."""
    op_type = op.get("op")
    if op_type not in ("add", "remove"):
        logger.warning("schedule.pending_op.unknown", op=op_type)
        return

    order = _op_to_scheduling_order(op)
    if op_type == "remove":
        result = remove_order(state, order)
        if result.status == "success":
            return
        logger.warning(
            "schedule.run.remove_failed",
            order_id=str(order.order_id),
            status=result.status,
            message=result.message,
        )
        requested_by = op.get("requested_by")
        if requested_by:
            websocket.notify_user(
                user_id=uuid.UUID(requested_by),
                message={
                    "type": "schedule.remove_failed",
                    "order_id": str(order.order_id),
                    "order_number": order.order_number,
                    "reason": result.status,
                    "detail": result.message,
                },
            )
        return

    # op_type == "add"
    result = add_order(state, order)
    if result.status == "success":
        return
    logger.warning(
        "schedule.run.add_failed",
        order_id=str(order.order_id),
        status=result.status,
        message=result.message,
    )
    requested_by = op.get("requested_by")
    if requested_by:
        websocket.notify_user(
            user_id=uuid.UUID(requested_by),
            message={
                "type": "schedule.add_failed",
                "order_id": str(order.order_id),
                "order_number": order.order_number,
                "reason": result.status,
                "detail": result.message,
            },
        )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _finalize_run(state: SchedulerState) -> int:
    """Compute schedule, persist to DB, save state, broadcast.

    Shared by the three tasks that mutate state — ``run_scheduling_task``
    (per-op), ``advance_day_task`` (after rolling the horizon), and
    ``rebuild_schedule_task`` (after rebuilding from DB). Each of these is
    the only writer of ``schedule:state`` in its body, so calling this once
    at the end is correct.

    Returns the number of scheduled rows written, for logging.
    """
    scheduled = compute_schedule(state)

    db: Session = SessionLocal()
    try:
        order_service.apply_schedule(db, scheduled)
    finally:
        db.close()

    _save_state(state)
    websocket.broadcast({"type": "schedule.updated"})
    return len(scheduled)


@celery_app.task(bind=True, name="scheduling.run")  # type: ignore[untyped-decorator]
def run_scheduling_task(self: Task) -> None:
    """Process **one** pending op, persist, broadcast, and re-fire if more.

    Each task invocation:

    1. Sets ``schedule:status`` to ``running``.
    2. ``ZPOPMIN``s the highest-priority op (shrink before grow, FIFO inside
       each group). If the queue is empty, sets status back to ``idle`` and
       returns without computing anything — no state change, no broadcast.
    3. Otherwise: applies the op to the in-memory state via ``_process_one``
       (``add_order`` / ``remove_order`` plus per-op ``notify_user`` on
       failure), then ``_finalize_run`` (compute schedule → write DB →
       persist state to Redis → broadcast ``schedule.updated``).
    4. Flips status back to ``idle``.
    5. If more ops are still pending, ``self.delay()`` to fire another
       invocation.

    **Per-op design rationale**: between every op the status flips to
    ``idle``, which lets ``advance_day_task`` / ``rebuild_schedule_task``
    (both polling status) slip in after the *current* op finishes — they
    no longer have to wait for the entire pending queue to drain. The
    cost is that ``compute_schedule`` + ``apply_schedule`` + broadcast
    runs N times instead of once for N ops; this trade-off is intentional
    so the frontend gets per-op refresh signals and rebuilds can interleave
    promptly with normal scheduling work.

    On any exception the status is flipped to ``failed`` and re-raised so
    Celery records the traceback.
    """
    started_at = datetime.now(tz=UTC).isoformat()
    task_id = str(self.request.id) if self.request.id else None
    _set_status(state=_STATUS_RUNNING, started_at=started_at, task_id=task_id)
    logger.info("schedule.run.start", task_id=task_id)

    try:
        op = _pop_next_op()

        if op is None:
            # Empty queue — flip back to idle without touching state.
            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_IDLE,
                started_at=started_at,
                finished_at=finished_at,
                task_id=task_id,
            )
            logger.info("schedule.run.empty_queue", task_id=task_id)
            return

        state = _load_state()
        _process_one(state, op)
        scheduled_rows = _finalize_run(state)

        finished_at = datetime.now(tz=UTC).isoformat()
        _set_status(
            state=_STATUS_IDLE,
            started_at=started_at,
            finished_at=finished_at,
            task_id=task_id,
        )
        logger.info(
            "schedule.run.success",
            task_id=task_id,
            scheduled_rows=scheduled_rows,
        )

        # If more ops are queued (either pre-existing or arrived while we
        # were running), fire another invocation so the next op gets
        # processed without waiting for an external trigger.
        #
        # Exception: if a waiter task (advance_day / rebuild) has set the
        # waiter flag — meaning it's currently inside _wait_for_idle_run
        # observing our status flip to idle — yield the re-trigger to it.
        # The waiter will fire run_scheduling_task.delay() at the end of
        # its own body. This prevents the (us-just-re-triggered) task and
        # the waiter from racing on schedule:state writes.
        if cast("int", _get_redis().zcard(PENDING_OPS_KEY)) > 0:
            if _is_waiter_pending():
                logger.info("schedule.run.yield_to_waiter", task_id=task_id)
            else:
                logger.info("schedule.run.re_trigger", task_id=task_id)
                run_scheduling_task.delay()
    except Exception as exc:
        finished_at = datetime.now(tz=UTC).isoformat()
        _set_status(
            state=_STATUS_FAILED,
            started_at=started_at,
            finished_at=finished_at,
            task_id=task_id,
            error=str(exc),
        )
        logger.error(
            "schedule.run.failed",
            task_id=task_id,
            error=str(exc),
            exc_info=True,
        )
        raise


def _wait_for_idle_run(*, log_event: str) -> None:
    """Block until ``schedule:status`` is no longer ``running``, or 5 min elapse.

    Shared by ``advance_day_task`` and ``rebuild_schedule_task`` — both must
    let an in-flight ``run_scheduling_task`` finish before they mutate state.
    Times out and proceeds anyway after 5 minutes; better to act on slightly
    stale state than to skip the operation entirely.
    """
    deadline = time.monotonic() + _RUN_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = _get_status()
        if status is None or status.get("state") != _STATUS_RUNNING:
            return
        time.sleep(_RUN_WAIT_POLL_INTERVAL_SECONDS)
    logger.warning(log_event, timeout_seconds=_RUN_WAIT_TIMEOUT_SECONDS)


@celery_app.task(name="scheduling.advance_day")  # type: ignore[untyped-decorator]
def advance_day_task() -> None:
    """Roll the scheduler horizon forward one day at 00:00 UTC.

    Polls ``schedule:status`` for up to 5 minutes waiting for any in-flight
    ``run_scheduling_task`` to finish. After the wait window we proceed
    regardless — better to re-base on a slightly stale state than to skip
    the day rollover entirely.

    Calls ``_finalize_run`` directly (state changed, frontend needs to
    refresh) so we don't depend on ``run_scheduling_task`` to broadcast for
    us — under the per-op design, run_scheduling skips broadcast when the
    queue is empty.

    Sets the waiter flag for the entire duration of this task (set before
    ``_wait_for_idle_run``, cleared in ``finally``) so a concurrent
    ``run_scheduling_task`` finishing during our wait yields its re-trigger
    to us instead of racing.

    Also claims ``schedule:status = running`` for the duration of its own
    work (after the wait, before clearing in the inner ``finally``). Without
    this claim, the window between ``_wait_for_idle_run`` returning and
    ``_finalize_run`` completing reads ``idle`` to outside callers, so a
    concurrent ``POST /schedule/trigger`` would dispatch a
    ``run_scheduling_task`` that races our state writes — the waiter flag
    suppresses *re-triggers* but not *new dispatches*.
    """
    logger.info("schedule.advance_day.start")
    _set_waiter_flag()
    try:
        _wait_for_idle_run(log_event="schedule.advance_day.wait_timeout")

        started_at = datetime.now(tz=UTC).isoformat()
        _set_status(state=_STATUS_RUNNING, started_at=started_at)
        try:
            state = _load_state()
            new_state = advance_day(state)
            _finalize_run(new_state)

            logger.info(
                "schedule.advance_day.success",
                old_base=state.base_date.isoformat(),
                new_base=new_state.base_date.isoformat(),
                carried=len(new_state.priority_queue),
            )

            # Drain any pending ops on top of the new state. If the queue is
            # empty, this just sets status=idle and returns; finalize already
            # broadcast.
            if cast("int", _get_redis().zcard(PENDING_OPS_KEY)) > 0:
                run_scheduling_task.delay()
        finally:
            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_IDLE,
                started_at=started_at,
                finished_at=finished_at,
            )
    finally:
        _clear_waiter_flag()


@celery_app.task(name="scheduling.rebuild")  # type: ignore[untyped-decorator]
def rebuild_schedule_task() -> None:
    """Rebuild scheduler state from DB on top of the latest base_date.

    Like ``advance_day_task`` this waits for any in-flight ``run_scheduling_task``
    to finish first (so we don't race state writes), rebuilds from
    ``status='scheduled'`` rows in Postgres, persists the new state, fires a
    ``schedule.rebuild_skipped`` WebSocket notification to each skipped order's
    creator, then re-triggers ``run_scheduling_task`` so any pending ops
    queued during the wait get drained on top of the fresh state.

    Skipped orders are only those whose ``requested_delivery_date`` falls
    outside the 30-day horizon — this can happen for long-stuck scheduled
    orders whose deadline has been overtaken by ``base_date`` advancing.
    """
    logger.info("schedule.rebuild.start")
    _set_waiter_flag()
    try:
        _wait_for_idle_run(log_event="schedule.rebuild.wait_timeout")

        started_at = datetime.now(tz=UTC).isoformat()
        _set_status(state=_STATUS_RUNNING, started_at=started_at)
        try:
            raw = cast("str | None", _get_redis().get(STATE_KEY))
            if raw is not None:
                base_date = SchedulerState.from_json(raw).base_date
            else:
                base_date = datetime.now(tz=UTC).date()

            db: Session = SessionLocal()
            try:
                orders, creators = order_service.list_for_scheduler(db)
            finally:
                db.close()

            new_state, skipped = rebuild_state(orders, base_date)
            _finalize_run(new_state)

            for skip in skipped:
                creator_id = creators.get(skip.order_id)
                if creator_id is None:
                    continue
                websocket.notify_user(
                    user_id=creator_id,
                    message={
                        "type": "schedule.rebuild_skipped",
                        "order_id": str(skip.order_id),
                        "order_number": skip.order_number,
                        "reason": skip.reason,
                    },
                )

            logger.info(
                "schedule.rebuild.success",
                base_date=base_date.isoformat(),
                orders_added=len(new_state.priority_queue),
                orders_skipped=len(skipped),
            )

            # Drain any pending ops on top of the rebuilt state. If empty,
            # finalize already broadcast and we have nothing more to do.
            if cast("int", _get_redis().zcard(PENDING_OPS_KEY)) > 0:
                run_scheduling_task.delay()
        finally:
            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_IDLE,
                started_at=started_at,
                finished_at=finished_at,
            )
    finally:
        _clear_waiter_flag()
