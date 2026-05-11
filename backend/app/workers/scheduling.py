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
from redis.exceptions import ResponseError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.repositories import order as order_repo
from app.services import order as order_service
from app.services import websocket
from app.services.schedule_queue import enqueue_notify_user
from app.services.scheduling import (
    MATERIALIZE_NOTIFY_PENDING_KEY,
    MATERIALIZE_NOTIFY_PROCESSING_KEY,
    MATERIALIZE_RUNNING_KEY,
    PENDING_OPS_KEY,
    STATE_KEY,
    STATUS_KEY,
    ScheduleResult,
    SchedulerState,
    SchedulingOrder,
    add_order,
    advance_day,
    compute_schedule,
    pin_order,
    rebuild_state,
    remove_order,
    unpin_order,
)
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

__all__ = ["advance_day_task", "rebuild_schedule_task", "run_scheduling_task"]


# ---------------------------------------------------------------------------
# Status / waiter-flag constants (worker-internal)
# ---------------------------------------------------------------------------
#
# The cross-layer contract keys (``STATE_KEY`` / ``STATUS_KEY`` /
# ``PENDING_OPS_KEY`` / ``PENDING_OPS_SEQ_KEY``) and ``score_for_op`` live in
# ``app.services.scheduling`` — see the import block above. The waiter flag
# is purely an internal coordination knob between the three worker tasks and
# is not part of the API contract, so it stays here.

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

# advance_day_task and rebuild_schedule_task both wait at most this long for
# an in-flight run to finish before proceeding. Polling cadence is short
# enough to be responsive and long enough to keep Redis traffic negligible.
# Tunable via ``SCHEDULER_RUN_WAIT_TIMEOUT_SECONDS`` /
# ``SCHEDULER_RUN_WAIT_POLL_INTERVAL_SECONDS`` env vars.
_RUN_WAIT_TIMEOUT_SECONDS = get_settings().SCHEDULER_RUN_WAIT_TIMEOUT_SECONDS
_RUN_WAIT_POLL_INTERVAL_SECONDS = get_settings().SCHEDULER_RUN_WAIT_POLL_INTERVAL_SECONDS


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


def _pop_next_compound() -> dict[str, Any] | None:
    """Pop the highest-priority pending compound, or ``None`` if empty.

    Priority: shrink-group compounds before grow-group, FIFO within each
    group. Both invariants are encoded in the sorted-set score by the
    producer (``score_for_op``), so this function is just an atomic
    ``ZPOPMIN`` — O(log n) regardless of queue depth.

    Each sorted-set member is a JSON-serialized
    :class:`ScheduleCompoundRequest` (with ``_seq`` added by
    ``schedule_queue.enqueue_compound``). Worker processes the compound's
    ops atomically; a freshly-arrived shrink compound CANNOT interrupt
    a compound currently being processed — that's the atomicity guarantee
    that supersedes the per-op "shrink jumps grow" mid-run behavior.

    Malformed JSON entries (shouldn't happen given the schema-validated
    producer, but Redis itself doesn't enforce content) are silently
    discarded with a warning; the loop continues until it finds a valid
    compound or empties the queue.
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
            logger.warning("schedule.pending_compound.malformed", raw=member)
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
# Compound application
# ---------------------------------------------------------------------------


def _apply_op(state: SchedulerState, op: dict[str, Any]) -> ScheduleResult:
    """Dispatch a single leaf op to the appropriate algorithm entrypoint.

    Returns the ``ScheduleResult`` directly so the compound driver can
    detect failure and trigger snapshot rollback. Malformed ops (unknown
    ``op`` field, missing required keys) come back as ``capacity_exceeded``
    with a clear message — same failure path as a legitimate algorithm
    rejection, so the caller's rollback / WS code doesn't need to special-
    case them.
    """
    op_type = op.get("op")
    if op_type == "remove":
        return remove_order(state, _op_to_scheduling_order(op))
    if op_type == "add":
        return add_order(state, _op_to_scheduling_order(op))
    if op_type == "pin":
        fake_raw = op.get("fake_deadline")
        if fake_raw is None:
            # Schema layer should have rejected this at enqueue time; if it
            # still gets here the payload is malformed.
            return ScheduleResult(
                status="capacity_exceeded",
                order_id=uuid.UUID(op["order_id"]),
                message="op='pin' is missing fake_deadline.",
            )
        return pin_order(
            state,
            _op_to_scheduling_order(op),
            date.fromisoformat(fake_raw),
        )
    if op_type == "unpin":
        return unpin_order(state, uuid.UUID(op["order_id"]))
    # Unknown op_type — surface as a structured failure.
    logger.warning("schedule.compound.unknown_op", op=op_type)
    return ScheduleResult(
        status="capacity_exceeded",
        order_id=uuid.UUID(op["order_id"]) if op.get("order_id") else None,
        message=f"unknown op kind: {op_type!r}",
    )


def _notify_compound_failure(
    *,
    compound: dict[str, Any],
    failed_op_index: int,
    failed_op: dict[str, Any],
    result: ScheduleResult,
) -> None:
    """Log + WS-notify a failed compound (post-rollback).

    Envelope shape matches :class:`ScheduleCompoundFailedDetail` in
    ``app.schemas.schedule`` so the frontend has a structured contract.
    Only emits ``notify_user`` if ``requested_by`` is present on the
    compound — internal-only compounds (no human originator) get logged
    but not pushed to any WS recipient.
    """
    logger.warning(
        "schedule.compound.failed",
        compound_id=compound.get("compound_id"),
        failed_op_index=failed_op_index,
        failed_op=failed_op.get("op"),
        status=result.status,
        message=result.message,
    )
    requested_by = compound.get("requested_by")
    if not requested_by:
        return
    websocket.notify_user(
        user_id=uuid.UUID(requested_by),
        message={
            "type": "schedule.compound_failed",
            "compound_id": str(compound.get("compound_id")),
            "failed_op_index": failed_op_index,
            "failed_op": failed_op.get("op"),
            "order_id": str(failed_op.get("order_id")),
            "order_number": failed_op.get("order_number"),
            "reason": result.status,
            "detail": result.message,
            "rolled_back": True,
        },
    )


def _restore_state_in_place(state: SchedulerState, snapshot_json: str) -> None:
    """Mutate *state* in-place to match the JSON snapshot.

    Used by the compound driver to roll back to pre-compound state on any
    leaf-op failure. We can't simply ``state = SchedulerState.from_json(...)``
    because that just rebinds the local name; callers hold a reference and
    expect the same object's internals to be updated.
    """
    restored = SchedulerState.from_json(snapshot_json)
    state.capacity_tree = restored.capacity_tree
    state.deadline_tree = restored.deadline_tree
    state.priority_queue = restored.priority_queue
    state.pinned_orders = restored.pinned_orders
    state.base_date = restored.base_date


def _process_compound(
    state: SchedulerState,
    compound: dict[str, Any],
) -> bool:
    """Apply a compound's ops in order, with snapshot-based saga rollback.

    Steps:

    1. Take a JSON snapshot of *state* (cheap — segment trees serialize as
       small int arrays).
    2. Iterate ops in order. Each calls into the appropriate algorithm
       function (``add_order`` / ``remove_order`` / ``pin_order`` /
       ``unpin_order``) and returns a :class:`ScheduleResult`.
    3. **Any single leaf op returning non-success** → restore state from
       the snapshot, emit ``schedule.compound_failed``, return ``False``.
       No partial mutation is observable past this function's return.
    4. All ops succeed → return ``True``. Caller will then ``_finalize_run``
       to compute + persist + broadcast.

    The whole compound is treated as one atomic unit; ``schedule:status``
    stays ``running`` for the entire span (set by the caller before this
    function is invoked, cleared after). That's the key invariant that lets
    ``advance_day`` / ``rebuild`` no longer slip into the middle of a
    compound — a problem the previous per-op design had.
    """
    snapshot = state.to_json()
    ops: list[dict[str, Any]] = compound.get("ops", [])

    # Tamper / truncation guard: payload self-declares its op count.
    # Schema validation at enqueue time already enforced this, but the
    # Redis member could in principle be corrupted post-enqueue (manual
    # surgery, partial write, etc.). A mismatch here means we have no
    # idea what we're processing — fail the whole compound up front
    # rather than execute a half-truncated business action.
    declared_op_count = compound.get("op_count")
    if declared_op_count is not None and declared_op_count != len(ops):
        logger.warning(
            "schedule.compound.op_count_mismatch",
            compound_id=compound.get("compound_id"),
            declared=declared_op_count,
            actual=len(ops),
        )
        sentinel_op = {
            "op": ops[0]["op"] if ops else "add",
            "order_id": ops[0]["order_id"] if ops else None,
            "order_number": ops[0].get("order_number", "") if ops else "",
        }
        _notify_compound_failure(
            compound=compound,
            failed_op_index=-1,
            failed_op=sentinel_op,
            result=ScheduleResult(
                status="capacity_exceeded",
                message=(
                    f"Compound payload corrupted: op_count={declared_op_count} "
                    f"but ops list has {len(ops)} entries."
                ),
            ),
        )
        return False

    for i, op in enumerate(ops):
        result = _apply_op(state, op)
        if result.status != "success":
            _restore_state_in_place(state, snapshot)
            _notify_compound_failure(
                compound=compound,
                failed_op_index=i,
                failed_op=op,
                result=result,
            )
            return False

    logger.info(
        "schedule.compound.success",
        compound_id=compound.get("compound_id"),
        op_count=len(ops),
    )
    return True


def _drop_compound_index_entry(compound_id: str | None) -> None:
    """Best-effort cleanup of the by-compound-id secondary index.

    The index is maintained by ``schedule_queue.enqueue_compound`` so a
    future cancel-by-compound-id endpoint can ``ZREM`` in O(1). Once we
    ``ZPOPMIN`` a compound, the cancellation window is closed and the
    index entry is stale — best-effort remove keeps Redis from growing
    unboundedly.
    """
    if not compound_id:
        return
    try:
        _get_redis().hdel("schedule:pending_ops:by_compound_id", compound_id)
    except Exception as exc:
        # Logging only; a stale index entry doesn't break correctness.
        logger.warning(
            "schedule.compound.index_cleanup_failed",
            compound_id=compound_id,
            error=str(exc),
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

    The pinned-orders map is derived from ``state.pinned_orders`` and
    threaded through to ``apply_schedule`` so DB rows for pinned orders end
    up with ``is_pinned=true`` and ``pinned_production_date=fake_deadline``.

    Returns the number of scheduled rows written, for logging.
    """
    scheduled = compute_schedule(state)
    pinned_map = {p.order_id: p.fake_deadline for p in state.pinned_orders.values()}

    db: Session = SessionLocal()
    try:
        order_service.apply_schedule(db, scheduled, pinned_map)
    finally:
        db.close()

    _save_state(state)
    websocket.broadcast({"type": "schedule.updated"})
    return len(scheduled)


@celery_app.task(bind=True, name="scheduling.run")  # type: ignore[untyped-decorator]
def run_scheduling_task(self: Task) -> None:
    """Process **one compound** end-to-end, then re-fire if more queued.

    **Phase 4 fast/slow split**: this task is the fast path. It does the
    in-memory state mutation (O(log n)·N per compound thanks to
    SortedKeyList + pq_index) plus the small ``_save_state`` to Redis,
    then immediately emits ``schedule.compound_accepted`` so the producer
    knows their compound was accepted. **It does NOT run
    ``compute_schedule`` / ``apply_schedule`` / DB writes** — those are
    offloaded to ``materialize_schedule_task`` which self-coalesces so
    bursts of compounds collapse into one DB rewrite. The user spec calls
    out the goal: accept/reject feedback in O(log n)·N instead of being
    gated on N DB round-trips per compound.

    Each task invocation:

    1. Sets ``schedule:status`` to ``running``.
    2. ``ZPOPMIN``s the highest-priority compound (shrink-group compounds
       sort before grow, FIFO within each group). If the queue is empty,
       sets status back to ``idle`` and returns — no state change, no
       notify.
    3. Otherwise: applies the compound atomically via ``_process_compound``
       (saga rollback on any leaf-op failure). On success:
       ``_save_state`` to Redis, ``notify_user(schedule.compound_accepted)``
       to ``requested_by``, ``enqueue_notify_user`` for the deferred
       materializer to notify post-DB-write, and
       ``materialize_schedule_task.delay()``. On failure, state is rolled
       back, ``schedule.compound_failed`` is notified inside
       ``_process_compound`` — no state save, no materialize trigger.
    4. Flips status back to ``idle``.
    5. If more compounds are still pending, ``self.delay()`` to fire another
       invocation (unless a waiter has set the ``schedule:waiter_pending``
       flag, in which case yield to the waiter).

    **Compound atomicity invariant** (replaces the old per-op invariant):
    ``schedule:status`` stays ``running`` for the entire compound — every
    leaf op inside the compound runs without ``advance_day`` /
    ``rebuild_schedule`` getting a chance to slip in. The trade-off vs.
    per-op design: a long compound holds the lock longer, but the
    correctness benefit (no cross-action interleaving in trees) outweighs
    that.

    On any exception (not a leaf-op failure — those are caught and turn
    into rollback — but a genuine Python exception like a Redis outage)
    the status is flipped to ``failed`` and re-raised so Celery records
    the traceback. ``failed`` doesn't block subsequent ``/trigger`` calls
    because the 409 logic only checks ``running``.
    """
    started_at = datetime.now(tz=UTC).isoformat()
    task_id = str(self.request.id) if self.request.id else None
    _set_status(state=_STATUS_RUNNING, started_at=started_at, task_id=task_id)
    logger.info("schedule.run.start", task_id=task_id)

    try:
        compound = _pop_next_compound()

        if compound is None:
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

        # Best-effort secondary-index cleanup BEFORE applying — the compound
        # is no longer cancellable now that we've popped it.
        _drop_compound_index_entry(compound.get("compound_id"))

        state = _load_state()
        applied = _process_compound(state, compound)

        if applied:
            # Fast-path success: persist state to Redis (O(n) serialize, but
            # tiny in absolute terms — just int arrays + small pq dump) and
            # notify the requester *immediately* that the compound was
            # accepted. The heavy compute_schedule + apply_schedule + DB
            # write is offloaded to ``materialize_schedule_task``; this is
            # what keeps the producer's accept/reject feedback O(log n)·N
            # instead of being gated on N DB round-trips per compound.
            _save_state(state)
            requested_by_raw = compound.get("requested_by")
            if requested_by_raw:
                websocket.notify_user(
                    user_id=uuid.UUID(requested_by_raw),
                    message={
                        "type": "schedule.compound_accepted",
                        "compound_id": str(compound.get("compound_id")),
                    },
                )
                # Defer DB materialization + per-user refetch notify.
                enqueue_notify_user(uuid.UUID(requested_by_raw))
            materialize_schedule_task.delay()
            logger.info(
                "schedule.run.success",
                task_id=task_id,
                compound_id=compound.get("compound_id"),
            )
        else:
            # Compound rolled back. No state change, no materialize trigger
            # — the WS notify_user (compound_failed) already fired inside
            # ``_process_compound``.
            logger.info(
                "schedule.run.rolled_back",
                task_id=task_id,
                compound_id=compound.get("compound_id"),
            )

        finished_at = datetime.now(tz=UTC).isoformat()
        _set_status(
            state=_STATUS_IDLE,
            started_at=started_at,
            finished_at=finished_at,
            task_id=task_id,
        )

        # If more compounds are queued (either pre-existing or arrived
        # while we were running), fire another invocation so the next
        # compound gets processed without waiting for an external trigger.
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


# ---------------------------------------------------------------------------
# materialize_schedule_task — Phase 4 deferred DB writer
# ---------------------------------------------------------------------------


_MATERIALIZE_RUNNING_TTL_SECONDS = 300  # 5 min safety window for crash recovery


@celery_app.task(name="scheduling.materialize")  # type: ignore[untyped-decorator]
def materialize_schedule_task() -> None:
    """Drain pending notifications and write the schedule to DB.

    Phase 4 slow path. Self-coalescing: many ``run_scheduling_task`` fast-
    path successes can collapse into one materializer run, so a burst of N
    compounds doesn't cause N full DB rewrites.

    Coordination via three Redis keys (see ``services.scheduling`` for the
    constants):

    * ``MATERIALIZE_RUNNING_KEY``: ``SET NX EX 300``. Only one materializer
      runs at a time. If we don't get the slot, exit silently — the
      currently-running task will re-trigger us if needed.
    * ``MATERIALIZE_NOTIFY_PENDING_KEY``: SADD'd by ``run_scheduling_task``
      with each successful compound's ``requested_by``. The set is the
      "to-notify" backlog.
    * ``MATERIALIZE_NOTIFY_PROCESSING_KEY``: in-flight batch. Atomic swap
      via ``RENAME`` ensures the worker drains a consistent snapshot;
      concurrent fast-path SADDs land in a fresh notify_pending set and
      get picked up on the next loop iteration (or a re-triggered run).

    Loop body per iteration:

    1. ``RENAME notify_pending → notify_processing``. If pending was empty
       the rename raises ``ResponseError`` and we exit the loop.
    2. Read the captured user IDs (``SMEMBERS notify_processing``).
    3. ``_load_state()`` → ``compute_schedule(state)`` →
       ``order_service.apply_schedule(db, scheduled, pinned_map)``. Writes
       the latest in-memory schedule to DB.
    4. For each captured user: ``notify_user(schedule.materialized)`` so
       their frontend refetches ``GET /schedule/result`` and sees the
       fresh DB rows.
    5. ``DEL notify_processing``.
    6. Loop again — concurrent fast tasks may have repopulated
       notify_pending while we were working.

    On exception inside the loop, the in-flight batch is merged back into
    ``notify_pending`` (``SUNIONSTORE``) so we don't lose users on retry.

    On normal exit (loop break), release the running flag and check one
    last time — if more arrived between the loop's final empty rename and
    our flag release, ``.delay()`` ourselves so the next worker picks
    them up.
    """
    rds = _get_redis()

    # Single-flight claim. Multiple delayed invocations cooperate via NX.
    claimed = rds.set(
        MATERIALIZE_RUNNING_KEY,
        "1",
        nx=True,
        ex=_MATERIALIZE_RUNNING_TTL_SECONDS,
    )
    if not claimed:
        logger.info("schedule.materialize.skip_concurrent")
        return

    try:
        # Crash recovery: salvage anything left in notify_processing from a
        # previous run that died mid-iteration. SUNIONSTORE merges the two
        # sets, DEL clears the processing key.
        if rds.exists(MATERIALIZE_NOTIFY_PROCESSING_KEY):
            rds.sunionstore(
                MATERIALIZE_NOTIFY_PENDING_KEY,
                [MATERIALIZE_NOTIFY_PENDING_KEY, MATERIALIZE_NOTIFY_PROCESSING_KEY],
            )
            rds.delete(MATERIALIZE_NOTIFY_PROCESSING_KEY)

        while True:
            try:
                rds.rename(
                    MATERIALIZE_NOTIFY_PENDING_KEY,
                    MATERIALIZE_NOTIFY_PROCESSING_KEY,
                )
            except ResponseError:
                # notify_pending didn't exist → no work this iteration.
                break

            users_raw = cast(
                "set[str]",
                rds.smembers(MATERIALIZE_NOTIFY_PROCESSING_KEY),
            )

            try:
                state = _load_state()
                scheduled_results = compute_schedule(state)
                pinned_map = {
                    p.order_id: p.fake_deadline
                    for p in state.pinned_orders.values()
                }
                db: Session = SessionLocal()
                try:
                    order_service.apply_schedule(
                        db, scheduled_results, pinned_map
                    )
                finally:
                    db.close()

                for user_raw in users_raw:
                    try:
                        websocket.notify_user(
                            user_id=uuid.UUID(user_raw),
                            message={"type": "schedule.materialized"},
                        )
                    except ValueError:
                        # Bad UUID slipped into the set somehow — log and
                        # move on, don't block the batch on one bad entry.
                        logger.warning(
                            "schedule.materialize.bad_user_id",
                            user=user_raw,
                        )

                rds.delete(MATERIALIZE_NOTIFY_PROCESSING_KEY)
                logger.info(
                    "schedule.materialize.batch_done",
                    notified=len(users_raw),
                    scheduled_rows=len(scheduled_results),
                )
            except Exception:
                # Restore the batch so the next run can retry. We don't
                # want to silently lose users on a transient DB outage.
                rds.sunionstore(
                    MATERIALIZE_NOTIFY_PENDING_KEY,
                    [MATERIALIZE_NOTIFY_PENDING_KEY, MATERIALIZE_NOTIFY_PROCESSING_KEY],
                )
                rds.delete(MATERIALIZE_NOTIFY_PROCESSING_KEY)
                raise
    finally:
        rds.delete(MATERIALIZE_RUNNING_KEY)

    # Post-release check: a fast task may have SADD'd between our final
    # empty rename and the running-flag DEL. Re-trigger so the new pending
    # users don't sit idle until the next fast task fires its own .delay().
    if rds.exists(MATERIALIZE_NOTIFY_PENDING_KEY):
        logger.info("schedule.materialize.re_trigger")
        materialize_schedule_task.delay()


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

    **Phase 3 — DB status workflow**. At each fire the task does two extra
    UPDATEs alongside the existing finalize:

    1. **``in_production → completed``** for orders that WERE locked in
       on a previous day and are no longer in the new scheduler state's
       living set (= ``priority_queue + pinned_orders``). Signal: those
       orders' last remaining work was made yesterday, they're done.
    2. **(scheduled | …) → ``in_production``** for orders that have any
       production assigned to "today" (the day-1 of the OLD state, which
       advance_day removes / partially consumes). This includes pinned-
       today, fully-done pq, and boundary orders. We snapshot the
       today-set from ``compute_schedule(old_state)`` BEFORE running
       advance_day, since after advance_day base_date has shifted and
       day-1 of new_state is tomorrow.

    The override order is: ``apply_schedule`` first (writes
    ``scheduled_production_date`` / ``expected_delivery_date`` /
    ``status='scheduled'`` for orders in the new state) → then
    ``mark_completed_outside_set`` → then ``mark_in_production`` last (so
    boundary orders which apply_schedule wrote as ``scheduled`` get
    correctly upgraded to ``in_production``).

    Sets the waiter flag for the entire duration (set before
    ``_wait_for_idle_run``, cleared in outer ``finally``) so a concurrent
    ``run_scheduling_task`` finishing during our wait yields its re-trigger
    to us instead of racing.

    Also claims ``schedule:status = running`` for the duration. On clean
    completion the inner success path writes ``idle``; on any exception the
    inner ``except`` writes ``failed`` (with the error string captured in
    ``schedule:status.error``) and re-raises so Celery still records the
    traceback.
    """
    logger.info("schedule.advance_day.start")
    _set_waiter_flag()
    try:
        _wait_for_idle_run(log_event="schedule.advance_day.wait_timeout")

        started_at = datetime.now(tz=UTC).isoformat()
        _set_status(state=_STATUS_RUNNING, started_at=started_at)
        try:
            state = _load_state()

            # Snapshot "today's locked-in" set BEFORE advance_day shifts
            # state. compute_schedule(state) gives us every order with any
            # work on day-1 of the OLD state (= today by definition of the
            # 00:00 UTC fire). Filter by scheduled_date == state.base_date
            # to be explicit.
            today = state.base_date
            today_locked_in_ids: set[uuid.UUID] = {
                sr.order_id
                for sr in compute_schedule(state)
                if sr.scheduled_date == today
            }

            new_state = advance_day(state)

            # Orders still alive in the new state — used by
            # ``mark_completed_outside_set`` to decide which currently-
            # in_production rows are done.
            new_alive_ids: set[uuid.UUID] = (
                {o.order_id for o in new_state.priority_queue}
                | set(new_state.pinned_orders.keys())
            )

            # Combined DB workflow: apply_schedule + status flips. We bypass
            # ``_finalize_run`` here so we can serialize the three DB writes
            # in one session before ``_save_state`` + broadcast.
            scheduled_results = compute_schedule(new_state)
            pinned_map = {
                p.order_id: p.fake_deadline for p in new_state.pinned_orders.values()
            }
            db: Session = SessionLocal()
            try:
                # 1. Set scheduled_production_date / dates / pin columns
                #    for orders still in state (status=scheduled).
                order_service.apply_schedule(db, scheduled_results, pinned_map)
                # 2. Yesterday's in_production orders that have no remaining
                #    work in state → completed.
                completed_count = order_repo.mark_completed_outside_set(
                    db, new_alive_ids
                )
                # 3. Today's-locked-in (advance_day moved them out of state)
                #    → in_production. Overrides apply_schedule's
                #    ``scheduled`` for any boundary order present here.
                in_prod_count = order_repo.mark_in_production(
                    db, today_locked_in_ids
                )
                db.commit()
            finally:
                db.close()

            _save_state(new_state)
            websocket.broadcast({"type": "schedule.updated"})

            logger.info(
                "schedule.advance_day.success",
                old_base=state.base_date.isoformat(),
                new_base=new_state.base_date.isoformat(),
                carried=len(new_state.priority_queue),
                in_production_count=in_prod_count,
                completed_count=completed_count,
            )

            # Drain any pending compounds on top of the new state. If the
            # queue is empty, finalize already broadcast and we have nothing
            # more to do.
            if cast("int", _get_redis().zcard(PENDING_OPS_KEY)) > 0:
                run_scheduling_task.delay()

            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_IDLE,
                started_at=started_at,
                finished_at=finished_at,
            )
        except Exception as exc:
            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_FAILED,
                started_at=started_at,
                finished_at=finished_at,
                error=str(exc),
            )
            logger.error(
                "schedule.advance_day.failed",
                error=str(exc),
                exc_info=True,
            )
            raise
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

    Status handling mirrors ``advance_day_task``: clean exit writes ``idle``,
    any exception inside the body writes ``failed`` with the error message
    and re-raises so Celery records the traceback. See ``advance_day_task``
    docstring for the rationale.
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

            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_IDLE,
                started_at=started_at,
                finished_at=finished_at,
            )
        except Exception as exc:
            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_FAILED,
                started_at=started_at,
                finished_at=finished_at,
                error=str(exc),
            )
            logger.error(
                "schedule.rebuild.failed",
                error=str(exc),
                exc_info=True,
            )
            raise
    finally:
        _clear_waiter_flag()
