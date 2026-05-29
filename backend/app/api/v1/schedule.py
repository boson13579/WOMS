"""Scheduling HTTP router.

Endpoints
---------
- ``POST   /schedule/trigger`` — manual fire of ``run_scheduling_task``.
- ``DELETE /schedule/operations/{compound_id}`` — cancel a still-queued compound.
- ``GET    /schedule/status`` — current scheduler lifecycle snapshot.
- ``GET    /schedule/result`` — every order currently in ``scheduled`` state.
- ``GET    /schedule/pending-ops`` — queued compounds in priority order.
- ``GET    /schedule/capacity`` — 30-day prefix-sum capacity snapshot.
- ``GET    /schedule/capacity-usage`` — 30-day realized used/remaining per day.
- ``POST   /schedule/rebuild`` — queue ``rebuild_schedule_task`` (waits for any
  in-flight run to finish, rebuilds state from DB, then re-triggers
  ``run_scheduling_task``).

The previous ``POST /schedule/operations`` raw-compound endpoint has been
removed — pin / unpin is now folded into ``PATCH /orders/{id}`` via the
``pinned_production_date`` field so it inherits the same row-level lock
that protects qty / deadline changes. See the comment block where the
endpoint used to live for the full rationale.

All Redis access goes through a lazy module-level client; the worker module
does the same so the two stay decoupled (worker can run without the API
process and vice versa).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any, TypeAlias, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
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
    ScheduleCapacityUsageResponse,
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
    list_pending_ops,
)
from app.services.scheduling import (
    DAILY_CAPACITY,
    HORIZON_DAYS,
    REBUILD_IN_FLIGHT_KEY,
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

logger = structlog.get_logger(__name__)

# Same role gates as orders.py.
_READ_ROLES = require_roles(UserRole.order_manager, UserRole.scheduler, UserRole.root)
_WRITE_ROLES = require_roles(UserRole.scheduler, UserRole.root)

_StrOrNone: TypeAlias = str | None


# ---------------------------------------------------------------------------
# Lazy Redis client
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _redis() -> Redis:
    """Module-level Redis client; instantiated on first use."""
    return Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True)


def _read_status() -> dict[str, Any] | None:
    raw = cast(_StrOrNone, _redis().get(STATUS_KEY))
    if raw is None:
        return None
    return cast("dict[str, Any]", json.loads(raw))


# ---------------------------------------------------------------------------
# POST /trigger
# ---------------------------------------------------------------------------


@router.post(
    "/trigger",
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_scheduling(
    current_user: Annotated[User, Depends(_WRITE_ROLES)],
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
# POST /operations — REMOVED
# ---------------------------------------------------------------------------
#
# The raw "build a compound, send it" endpoint used to be the only way for
# the frontend to issue pin / unpin. It had a structural weakness: it
# accepted any client-constructed compound and enqueued it directly,
# bypassing the per-row ``SELECT ... FOR UPDATE`` lock that ``update_order``
# / ``delete_order`` use to serialize concurrent producer-side writes. Two
# concurrent compounds for the same order could both build off the same
# pre-PATCH snapshot, both enqueue, and the worker would later trip a
# ``SegmentTreeInvariantError`` on the stale half (because the in-memory
# tree no longer matched the compound's ``wafer_quantity``).
#
# Pin / unpin is now folded into ``PATCH /orders/{id}`` via the
# ``pinned_production_date`` field — that path goes through ``update_order``
# which holds the row lock for the whole producer transaction and emits the
# right ``[unpin?, remove?, add?, pin?]`` compound shape based on the pin
# transition. Frontend ``OrdersCalendarDialog`` calls PATCH directly; the
# raw endpoint has no remaining caller.
#
# The internal ``enqueue_compound`` service function is unchanged — it's
# still the single Redis writer used by the order CRUD service layer.
# Only the *HTTP wrapper* is removed.


# ---------------------------------------------------------------------------
# DELETE /operations/{compound_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/operations/{compound_id}",
)
def cancel_compound_endpoint(
    compound_id: uuid.UUID,
    current_user: Annotated[User, Depends(_WRITE_ROLES)],
) -> ScheduleCompoundResponse:
    """Cancel a still-queued scheduler compound.

    Looks up the compound by id in the
    ``schedule:pending_ops:by_compound_id`` secondary index, ``ZREM``s it
    from the sorted set, runs the producer-pre-write compensation
    (``perform_compound_db_action(accepted=False)``), and fires
    ``schedule.compound_cancelled`` to the compound's ``requested_by``.

    Returns:
        ``200`` — compound was in queue and got removed.
        ``409`` — compound was in the index but the worker popped it
                 between our lookup and our ``ZREM`` (already in flight).
                 The frontend should fall back to waiting for the regular
                 ``schedule.updated`` / ``schedule.compound_failed`` outcome.
        ``404`` — compound id is unknown (never enqueued, or processed
                 long enough ago that the index entry was cleaned).
        ``500`` — ZREM succeeded but the DB compensation step failed
                 (deadlock / constraint violation / DB outage). The row
                 is stuck in producer-locked state; ops needs to inspect.
                 ``cancel_compound`` re-raises in this case rather than
                 lying to the user that cancellation worked.

    Permission: scheduler+.
    """
    try:
        result = cancel_compound(compound_id)
    except Exception as exc:
        # Compensation-failure path. ``cancel_compound`` logged with
        # ``row_state=locked_orphaned_in_db`` already; we surface a 500
        # so the frontend keeps the "processing" UI and the user knows
        # something went wrong. NOT a 409 — that semantically means
        # "racing worker won", which would falsely tell the user "retry
        # later". This is "we ate half your action; investigate."
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Cancellation partially completed: compound removed from queue "
                "but database compensation failed. Order may remain in "
                "processing state; please contact an administrator."
            ),
        ) from exc

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
)
def get_schedule_status(
    current_user: Annotated[User, Depends(_READ_ROLES)],
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
)
def get_schedule_result(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(_READ_ROLES)],
    include_completed: Annotated[
        bool,
        Query(
            description=(
                "Include ``completed`` orders alongside ``scheduled`` / "
                "``in_production``. The default is False to preserve legacy "
                "behavior; the calendar view passes True."
            ),
        ),
    ] = False,
    completed_since: Annotated[
        date | None,
        Query(
            description=(
                "Lower-bound (inclusive) on ``scheduled_production_date`` for "
                "completed orders. Ignored when ``include_completed`` is False. "
                "When ``include_completed`` is True and this is omitted, defaults "
                "to today - ``SCHEDULER_HORIZON_DAYS`` days."
            ),
        ),
    ] = None,
) -> list[ScheduleResultResponse]:
    """Return every order on the production timeline, with per-day breakdown.

    Sorted by ``scheduled_production_date`` ascending so the timeline is
    natural for the UI. Both the summary dates and the ``daily_breakdown``
    list come straight from Postgres — the columns are kept fresh by
    ``materialize_schedule_task``, which re-computes the schedule after
    every accepted compound and writes the per-day split into
    ``orders.daily_breakdown`` (JSONB). The Redis ``SchedulerState`` is
    NOT consulted on this read path; it stays a pure algorithm cache.

    Empty when no scheduler run has happened yet (column NULL → empty
    list).

    ``include_completed=true`` adds completed orders to the response,
    restricted to ``scheduled_production_date >= completed_since``.
    When ``completed_since`` is omitted the window defaults to
    ``today - SCHEDULER_HORIZON_DAYS`` so the visible history mirrors
    the forward planning horizon — same number of days in each
    direction keeps the calendar symmetric and bounds the response.

    Permission: order_manager+.
    """
    effective_since: date | None = None
    if include_completed:
        lookback_days = get_settings().SCHEDULER_HORIZON_DAYS
        effective_since = completed_since or (
            datetime.now(tz=UTC).date() - timedelta(days=lookback_days)
        )
    return order_service.list_scheduled_orders(db, completed_since=effective_since)


# ---------------------------------------------------------------------------
# GET /pending-ops
# ---------------------------------------------------------------------------


@router.get(
    "/pending-ops",
)
def get_pending_ops(
    current_user: Annotated[User, Depends(_READ_ROLES)],
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
)
def get_schedule_capacity(
    current_user: Annotated[User, Depends(_READ_ROLES)],
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
    raw = cast(_StrOrNone, _redis().get(STATE_KEY))
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
# GET /capacity-usage
# ---------------------------------------------------------------------------


@router.get(
    "/capacity-usage",
)
def get_schedule_capacity_usage(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(_READ_ROLES)],
) -> ScheduleCapacityUsageResponse:
    """Per-day used / remaining capacity across the upcoming 30-day horizon.

    Reads the ``schedule_daily_capacity`` snapshot the materializer
    writes after every accepted compound. Each entry says: "on this
    date, the EDF forward-fill schedule commits ``used`` wafers, leaving
    ``remaining = DAILY_CAPACITY - used`` free."

    Distinct from ``GET /schedule/capacity``, which returns the segment
    tree's backward-fill prefix sums from Redis — that one answers
    "is there room to admit a new order with this deadline?" (feasibility
    view), this one answers "for the currently-applied schedule, what
    does each day actually look like?" (realized view). The numbers can
    differ: a single 8,000-wafer order with deadline +7 days reserves
    capacity_tree backward from day 7, but forward-fill compute_schedule
    plans it on days 1-2.

    Always returns exactly ``HORIZON_DAYS`` entries starting at
    ``base_date``. We use the Redis ``SchedulerState.base_date`` so the
    series aligns with the scheduler's own calendar; if state is missing
    (first deploy / flushed Redis) we fall back to today so the dashboard
    still gets a usable response.

    Permission: order_manager+.
    """
    raw = cast(_StrOrNone, _redis().get(STATE_KEY))
    if raw is None:
        base_date = datetime.now(tz=UTC).date()
    else:
        base_date = SchedulerState.from_json(raw).base_date

    base, capacity, entries = order_service.get_capacity_usage(
        db,
        base_date=base_date,
        daily_capacity=DAILY_CAPACITY,
        horizon_days=HORIZON_DAYS,
    )
    return ScheduleCapacityUsageResponse(
        base_date=base,
        daily_capacity=capacity,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# POST /rebuild
# ---------------------------------------------------------------------------


@router.post(
    "/rebuild",
    status_code=status.HTTP_202_ACCEPTED,
)
def rebuild_schedule(
    current_user: Annotated[User, Depends(_WRITE_ROLES)],
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

    **Single-flight guard**: a rebuild can sit waiting up to the run-wait
    timeout (5 min) for an in-flight run to drain before it even starts its
    own work. Without a guard, spamming the rebuild button queues N
    ``rebuild_schedule_task`` instances that each wait + run serially —
    the pile-up blows past the frontend's request timeout and surfaces as
    "fail to load". So we claim ``REBUILD_IN_FLIGHT_KEY`` with ``SET NX EX``
    BEFORE dispatch (covers the dispatch → task-start gap) and reject a
    second concurrent rebuild with 409. The task clears the flag in its
    ``finally`` block; the TTL is a crash safety net only.

    The flag is set here (producer side) rather than inside the task
    because the task starts asynchronously — if we set it in the task,
    a burst of requests would all dispatch before the first task ran and
    claimed the flag, defeating the guard.

    Errors:
        409: a rebuild is already in progress (queued or running).

    Permission: scheduler+.
    """
    # TTL covers the worst-case rebuild lifetime (wait-for-idle + lock
    # acquire + rebuild) so a crashed worker that never reaches the
    # task's ``finally`` can't suppress rebuilds forever. Reuse the
    # waiter-flag TTL — same "crashed waiter" semantics.
    ttl = get_settings().SCHEDULER_WAITER_FLAG_TTL_SECONDS
    claimed = _redis().set(REBUILD_IN_FLIGHT_KEY, "1", nx=True, ex=ttl)
    if not claimed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A schedule rebuild is already in progress; please wait for it to finish.",
        )

    try:
        async_result = rebuild_schedule_task.delay()
    except Exception as exc:
        # Dispatch failed (broker down etc.) — release the flag so the
        # next request isn't wrongly rejected. The task never ran, so
        # nothing else will clear it (only the TTL would, eventually).
        _redis().delete(REBUILD_IN_FLIGHT_KEY)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not queue rebuild task (scheduler broker unavailable); please retry.",
        ) from exc

    return ScheduleRebuildResponse(
        task_id=str(async_result.id),
        message="Rebuild queued; will run after any in-flight scheduling completes.",
    )
