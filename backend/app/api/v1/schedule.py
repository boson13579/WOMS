"""Scheduling HTTP router.

Endpoints
---------
- ``POST /schedule/trigger`` — manual fire of ``run_scheduling_task``.
- ``POST /schedule/operations`` — Order CRUD pushes pending ops here.
- ``GET  /schedule/status`` — current scheduler lifecycle snapshot.
- ``GET  /schedule/result`` — every order currently in ``scheduled`` state.
- ``POST /schedule/rebuild`` — queue ``rebuild_schedule_task`` (waits for any
  in-flight run to finish, rebuilds state from DB, then re-triggers
  ``run_scheduling_task``).

All Redis access goes through a lazy module-level client; the worker module
does the same so the two stay decoupled (worker can run without the API
process and vice versa).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from redis import Redis
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import require_roles
from app.models.user import User, UserRole
from app.schemas.schedule import (
    CapacityPrefixEntry,
    PendingOpsEntry,
    ScheduleCapacityResponse,
    ScheduleCompoundRequest,
    ScheduleCompoundResponse,
    ScheduleRebuildResponse,
    ScheduleResultResponse,
    ScheduleStatusResponse,
    ScheduleTriggerResponse,
)
from app.services import order as order_service
from app.services.schedule_queue import (
    CancelResult,
    cancel_compound,
    enqueue_compound,
    list_pending_ops,
)
from app.services.scheduling import (
    DAILY_CAPACITY,
    STATE_KEY,
    STATUS_KEY,
    SchedulerState,
    capacity_prefix_sums,
)

# Workers are a peer of services; api → workers is allowed *only* for
# dispatching Celery task objects (``.delay()``). Anything else (Redis keys,
# encoding helpers, internal flags) lives in ``app.services.scheduling`` or
# ``app.services.schedule_queue``.
from app.workers.scheduling import (
    rebuild_schedule_task,
    run_scheduling_task,
)

router = APIRouter()

# Same role gates as orders.py.
_READ_ROLES = require_roles(UserRole.order_manager, UserRole.scheduler, UserRole.root)
_WRITE_ROLES = require_roles(UserRole.scheduler, UserRole.root)


# ---------------------------------------------------------------------------
# Lazy Redis client
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _redis() -> Redis:
    """Module-level Redis client; instantiated on first use."""
    return Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True)


def _read_status() -> dict[str, Any] | None:
    raw = cast("str | None", _redis().get(STATUS_KEY))
    if raw is None:
        return None
    return cast("dict[str, Any]", json.loads(raw))


# ---------------------------------------------------------------------------
# POST /trigger
# ---------------------------------------------------------------------------


@router.post(
    "/trigger",
    response_model=ScheduleTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_scheduling(
    current_user: User = Depends(_WRITE_ROLES),
) -> ScheduleTriggerResponse:
    """Manually dispatch a scheduling run.

    Permission: scheduler+.

    Errors:
        409: a run is already in progress.
    """
    status_doc = _read_status()
    if status_doc is not None and status_doc.get("state") == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scheduling already in progress",
        )

    async_result = run_scheduling_task.delay()
    return ScheduleTriggerResponse(
        task_id=str(async_result.id),
        message="Scheduling started",
    )


# ---------------------------------------------------------------------------
# POST /operations
# ---------------------------------------------------------------------------


@router.post(
    "/operations",
    response_model=ScheduleCompoundResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_operation(
    request: ScheduleCompoundRequest,
    current_user: User = Depends(_WRITE_ROLES),
) -> ScheduleCompoundResponse:
    """Queue a scheduler compound for the next ``run_scheduling_task``.

    A compound is an atomic business action containing one or more leaf
    ops (add / remove / pin / unpin). See :class:`ScheduleCompoundRequest`
    for the full contract; in brief:

    - Ops within a compound may target one or more order_ids — typical
      single-order flows (PATCH / DELETE / CREATE) generate one order
      per compound, while a multi-order batch business action is also
      a legal shape.
    - The compound has a single ``group`` (shrink or grow). Shrink
      compounds sort before grow compounds in the worker queue; FIFO
      within each group.
    - Worker processes the compound atomically — any leaf-op failure
      triggers a snapshot rollback of ``SchedulerState`` and a
      ``schedule.compound_failed`` WebSocket message to ``requested_by``.

    Backed by a Redis **sorted set** scored by ``score_for_op(group, seq)``
    where ``seq`` is the next value of ``schedule:pending_ops:seq``.
    Worker ``ZPOPMIN``s one compound per ``run_scheduling_task``
    invocation. All Redis I/O is delegated to
    ``services.schedule_queue.enqueue_compound``.

    A new ``run_scheduling_task`` fires only if the worker is currently
    idle; if it is already running, the in-flight task will pick up this
    compound when it loops back to drain ``pending_ops`` at the end of
    its cycle.

    Permission: scheduler+.
    """
    enqueue_compound(request)
    return ScheduleCompoundResponse(
        compound_id=request.compound_id,
        message="Compound queued",
    )


# ---------------------------------------------------------------------------
# DELETE /operations/{compound_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/operations/{compound_id}",
    response_model=ScheduleCompoundResponse,
)
def cancel_compound_endpoint(
    compound_id: uuid.UUID,
    current_user: User = Depends(_WRITE_ROLES),
) -> ScheduleCompoundResponse:
    """Cancel a still-queued scheduler compound.

    Looks up the compound by id in the
    ``schedule:pending_ops:by_compound_id`` secondary index, ``ZREM``s it
    from the sorted set, and fires ``schedule.compound_cancelled`` to the
    compound's ``requested_by``.

    Returns:
        ``200`` — compound was in queue and got removed.
        ``409`` — compound was in the index but the worker popped it
                 between our lookup and our ``ZREM`` (already in flight).
                 The frontend should fall back to waiting for the regular
                 ``schedule.updated`` / ``schedule.compound_failed`` outcome.
        ``404`` — compound id is unknown (never enqueued, or processed
                 long enough ago that the index entry was cleaned).

    Permission: scheduler+.
    """
    result = cancel_compound(compound_id)
    if result is CancelResult.cancelled:
        return ScheduleCompoundResponse(
            compound_id=compound_id,
            message="Compound cancelled",
        )
    if result is CancelResult.in_progress:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Compound is already in progress; cancellation lost the race.",
        )
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Compound not found in the pending queue.",
    )


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


@router.get(
    "/status",
    response_model=ScheduleStatusResponse,
)
def get_schedule_status(
    current_user: User = Depends(_READ_ROLES),
) -> ScheduleStatusResponse:
    """Current scheduler lifecycle state, mirrored from Redis.

    Permission: order_manager+.
    """
    status_doc = _read_status()
    if status_doc is None:
        return ScheduleStatusResponse(
            state="idle",
            message="No scheduling has been run yet",
        )
    return ScheduleStatusResponse.model_validate(status_doc)


# ---------------------------------------------------------------------------
# GET /result
# ---------------------------------------------------------------------------


@router.get(
    "/result",
    response_model=list[ScheduleResultResponse],
)
def get_schedule_result(
    db: Session = Depends(get_db),
    current_user: User = Depends(_READ_ROLES),
) -> list[ScheduleResultResponse]:
    """Return every order currently in ``scheduled`` status, with per-day breakdown.

    Sorted by ``scheduled_production_date`` ascending so the timeline is
    natural for the UI. Both the summary dates and the ``daily_breakdown``
    list come straight from Postgres — the columns are kept fresh by
    ``materialize_schedule_task``, which re-computes the schedule after
    every accepted compound and writes the per-day split into
    ``orders.daily_breakdown`` (JSONB). The Redis ``SchedulerState`` is
    NOT consulted on this read path; it stays a pure algorithm cache.

    Empty when no scheduler run has happened yet (column NULL → empty
    list).

    Permission: order_manager+.
    """
    return order_service.list_scheduled_orders(db)


# ---------------------------------------------------------------------------
# GET /pending-ops
# ---------------------------------------------------------------------------


@router.get(
    "/pending-ops",
    response_model=list[PendingOpsEntry],
)
def get_pending_ops(
    current_user: User = Depends(_READ_ROLES),
) -> list[PendingOpsEntry]:
    """Snapshot the worker's pending-compound queue with drain ranks.

    Each entry is one ``ScheduleCompoundRequest`` currently sitting in
    ``schedule:pending_ops`` (the Redis sorted set the worker drains via
    ``ZPOPMIN``). ``rank`` is 1-indexed and matches the order the worker
    will process them — rank=1 is "next to be processed".

    A compound may touch one OR more orders (a batch business action is
    legal); ``ops`` on each entry keeps the per-op order linkage. The
    dashboard answers "where is order X in line?" by scanning entries
    whose ``ops`` contain ``order_id == X`` and reading the smallest
    ``rank`` (an order can appear in multiple compounds if PATCHes pile
    up faster than the worker drains).

    Empty list when the queue is idle. Returns 200 either way so the
    dashboard can poll without special-casing "no data".

    Permission: order_manager+.
    """
    return list_pending_ops()


# ---------------------------------------------------------------------------
# GET /capacity
# ---------------------------------------------------------------------------


@router.get(
    "/capacity",
    response_model=ScheduleCapacityResponse,
)
def get_schedule_capacity(
    current_user: User = Depends(_READ_ROLES),
) -> ScheduleCapacityResponse:
    """Per-day prefix sum of remaining wafer capacity across the 30-day horizon.

    Reads the live ``SchedulerState`` from Redis and queries
    ``capacity_tree`` for each of the 30 day-indices. ``entries[i]``
    holds the prefix sum from ``base_date`` through ``base_date + i``
    days — i.e., how many wafers' worth of spare capacity exist
    cumulatively up to that day. Same source the segment tree itself
    uses to make feasibility decisions, so the number the dashboard
    shows always matches the scheduler's own view.

    No DB hit on this path: capacity is an algorithm-internal quantity
    and lives only in Redis. If the Redis state is missing (first
    deploy or a flush), we fabricate a fresh ``SchedulerState.initial``
    keyed on today so the dashboard still gets a usable 30-entry
    response (every day = ``DAILY_CAPACITY``, cumulative sum scaled
    accordingly) instead of an empty payload or 500.

    Permission: order_manager+.
    """
    raw = cast("str | None", _redis().get(STATE_KEY))
    if raw is None:
        state = SchedulerState.initial(datetime.now(tz=UTC).date())
    else:
        state = SchedulerState.from_json(raw)

    entries = [
        CapacityPrefixEntry(date=d, cumulative_remaining=prefix)
        for d, prefix in capacity_prefix_sums(state)
    ]
    return ScheduleCapacityResponse(
        base_date=state.base_date,
        daily_capacity=DAILY_CAPACITY,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# POST /rebuild
# ---------------------------------------------------------------------------


@router.post(
    "/rebuild",
    response_model=ScheduleRebuildResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def rebuild_schedule(
    current_user: User = Depends(_WRITE_ROLES),
) -> ScheduleRebuildResponse:
    """Queue a scheduler state rebuild from DB scheduled orders.

    Dispatches ``rebuild_schedule_task``, which:

    1. Waits (up to 5 minutes) for any in-flight ``run_scheduling_task`` to
       finish so the rebuild does not race state writes.
    2. Re-builds ``schedule:state`` from ``status='scheduled'`` rows in
       Postgres, sorted by ``sort_key()``.
    3. Sends a ``schedule.rebuild_skipped`` WebSocket message to each skipped
       order's creator (deadline overtaken by ``base_date`` etc.).
    4. Re-triggers ``run_scheduling_task`` so any pending ops queued during
       the wait are drained on top of the fresh state.

    No 409 is raised even when a run is in progress — the task self-serializes
    by polling status. This endpoint never blocks; results / skipped orders
    are surfaced via WebSocket events the caller subscribes to.

    Permission: scheduler+.
    """
    async_result = rebuild_schedule_task.delay()
    return ScheduleRebuildResponse(
        task_id=str(async_result.id),
        message="Rebuild queued; will run after any in-flight scheduling completes.",
    )
