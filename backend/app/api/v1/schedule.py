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
    ScheduleOperationRequest,
    ScheduleRebuildResponse,
    ScheduleResultResponse,
    ScheduleStatusResponse,
    ScheduleTriggerResponse,
)
from app.services import order as order_service
from app.services.scheduling import (
    ScheduledResult,
    SchedulerState,
    compute_schedule,
)
from app.workers.scheduling import (
    PENDING_OPS_KEY,
    PENDING_OPS_SEQ_KEY,
    STATE_KEY,
    STATUS_KEY,
    rebuild_schedule_task,
    run_scheduling_task,
    score_for_op,
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
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_operation(
    request: ScheduleOperationRequest,
    current_user: User = Depends(_WRITE_ROLES),
) -> dict[str, str]:
    """Queue an order op for the next scheduling run.

    Order CRUD calls this after persisting create / update / delete. Modifying
    a quantity or deadline must be split into ``remove`` (old values) followed
    by ``add`` (new values) — the algorithm has no native ``modify``.

    Backend writes to a Redis **sorted set** keyed by score
    ``score_for_op(group, seq)`` — shrink ops sort before grow, FIFO inside
    each group. ``seq`` is a per-op monotonic ``INCR`` so duplicate payloads
    don't collide on the sorted-set member key, and so the worker's
    ``ZPOPMIN`` is O(log n) regardless of queue depth.

    A new run fires only if the worker is currently idle; if it is already
    running, the in-flight task will pick up this op when it loops back to
    drain ``pending_ops`` at the end of its cycle.

    Permission: scheduler+.
    """
    rds = _redis()
    seq = cast("int", rds.incr(PENDING_OPS_SEQ_KEY))
    payload = request.model_dump(mode="json")
    payload["_seq"] = seq  # uniqueness guarantee for the sorted-set member
    score = score_for_op(group=request.group, seq=seq)
    rds.zadd(PENDING_OPS_KEY, {json.dumps(payload): score})

    status_doc = _read_status()
    if status_doc is None or status_doc.get("state") != "running":
        run_scheduling_task.delay()

    return {"message": "Operation queued"}


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
    natural for the UI. ``daily_breakdown`` is derived from the live
    ``SchedulerState`` in Redis via ``compute_schedule(state)`` — it
    reflects the same forward-fill assignment that produced the persisted
    ``scheduled_production_date`` / ``expected_delivery_date`` summary
    fields. Empty when no scheduler run has happened yet.

    Permission: order_manager+.
    """
    breakdown: list[ScheduledResult] = []
    raw = cast("str | None", _redis().get(STATE_KEY))
    if raw is not None:
        state = SchedulerState.from_json(raw)
        breakdown = compute_schedule(state)
    return order_service.list_scheduled_orders(db, breakdown=breakdown)


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
