"""FastAPI startup recovery — bring scheduler state into a sane post-restart shape.

Three known gaps after a server restart that the runtime can't self-heal:

1. **Stale ``base_date``** — Celery Beat fires ``advance_day_task`` at 00:00
   UTC. If the worker (or whole stack) was down across one or more
   midnights, those Beat ticks are missed and ``schedule:state.base_date``
   stays at the OLD day. Segment-tree day 1 is no longer "today" — every
   subsequent ``add_order`` / ``compute_schedule`` reasons against the wrong
   calendar.
2. **Missing ``schedule:state``** — Redis flushed / first deploy / state
   schema upgrade. Worker would fall back to an empty state and silently
   forget existing scheduled orders' load.
3. **Pending compound stuck after crash** — worker crashed mid-drain. The
   ``pending_ops`` queue still has entries but ``schedule:status`` may be
   stuck at ``running`` (the dead worker never wrote ``idle``) so a fresh
   ``run_scheduling_task.delay()`` won't be auto-triggered by the next
   producer.

This module runs once at FastAPI startup and dispatches the appropriate
recovery tasks. Everything is ``.delay()`` — non-blocking; FastAPI accepts
traffic immediately and the celery worker picks up the queued recovery
work as soon as it's ready.

**Idempotence across replicas**: ``SET NX EX 60`` on a recovery-running
flag. First FastAPI replica wins; others log + skip. The 60s TTL is the
upper bound on how long a single recovery dispatch should take (it's just
N queue submissions, not actual algorithm work).

**Best-effort, never raise**: any exception is caught + logged so that
recovery problems can't prevent the API from coming up. The cost of a
silent miss here is "next user action triggers the missing work anyway";
the cost of raising is "API container in CrashLoopBackoff".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import structlog
from redis import Redis

from app.core.config import get_settings
from app.services.schedule_queue import _read_status
from app.services.scheduling import (
    HORIZON_DAYS,
    PENDING_OPS_KEY,
    STATE_KEY,
    SchedulerState,
)

logger = structlog.get_logger(__name__)

__all__ = ["run_startup_recovery"]

# Mutex flag — only one FastAPI replica per cluster should dispatch
# recovery, otherwise an N-day catchup gets N replicas queueing N copies
# of advance_day_task and the state would over-advance.
_RECOVERY_FLAG_KEY = "schedule:startup_recovery_running"
_RECOVERY_FLAG_TTL_SECONDS = 60


def run_startup_recovery() -> None:
    """Dispatch recovery tasks for any gaps detected in Redis state.

    Safe to call from FastAPI lifespan: catches and logs all exceptions,
    returns ``None`` regardless. Returns quickly (Redis reads + a few
    ``.delay()`` calls) so it doesn't block the API from accepting traffic.

    Behavior matrix:

    | Detected condition | Action |
    |---|---|
    | ``schedule:state`` key missing | ``rebuild_schedule_task.delay()`` |
    | ``state.base_date < today``    | ``advance_day_task.delay()`` x N, capped at HORIZON_DAYS |
    | pending ops > 0 AND status idle / missing | ``run_scheduling_task.delay()`` |

    None of these are mutually exclusive — rebuild + advance_day catchup
    can both fire on a Redis-flushed-mid-downtime restart (rebuild lands
    state at today; advance_day catchup then becomes a no-op because
    base_date is already today).

    **Skipped under ``APP_ENV=test``**: pytest's ``TestClient(app)`` context
    manager triggers FastAPI lifespan startup for every ``client`` fixture
    instantiation, which on a freshly-flushed Redis would call ``.delay()``
    on Celery tasks ~40 times per CI run. The recovery semantics
    (catching missed advance_day ticks, rebuilding lost state, kicking
    orphan pending_ops) target real server restarts — none of those gaps
    can exist in an isolated test that sets its own Redis state. Tests
    that want to exercise the dispatcher itself bypass this guard by
    calling the underlying helper :func:`_dispatch_recovery` directly
    (see ``tests/services/test_startup_recovery.py``).
    """
    settings = get_settings()
    if settings.APP_ENV == "test":
        logger.debug("schedule.startup_recovery.skipped_test_env")
        return

    try:
        rds = _redis()
        if not _acquire_recovery_flag(rds):
            logger.info("schedule.startup_recovery.skipped_other_replica_running")
            return

        try:
            _dispatch_recovery(rds)
        finally:
            # Always release — TTL is just crash-safety for "we died
            # mid-dispatch", not the primary cleanup mechanism.
            rds.delete(_RECOVERY_FLAG_KEY)
    except Exception:
        # Never let recovery problems block API startup.
        logger.exception("schedule.startup_recovery.failed")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _redis() -> Redis:
    return Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True)


def _acquire_recovery_flag(rds: Redis) -> bool:
    """Try to claim the recovery-running mutex.

    Returns True iff this replica is the one that should run recovery.
    Uses ``SET NX EX`` for atomic check-and-set with auto-expiry.
    """
    return bool(rds.set(_RECOVERY_FLAG_KEY, "1", nx=True, ex=_RECOVERY_FLAG_TTL_SECONDS))


def _dispatch_recovery(rds: Redis) -> None:
    """Inspect Redis state and queue the appropriate recovery Celery tasks.

    Imports celery tasks lazily because importing ``app.workers.scheduling``
    at module load order pulls in celery + transitively the worker code
    paths; the FastAPI process only needs the producer side. Lazy import
    keeps the import graph clean and lets unit tests stub the workers.
    """
    from app.workers.scheduling import (  # noqa: PLC0415 — lazy by design
        advance_day_task,
        rebuild_schedule_task,
        run_scheduling_task,
    )

    today = datetime.now(tz=UTC).date()
    raw_state = cast("str | None", rds.get(STATE_KEY))

    # --- Step 1: state missing → rebuild ------------------------------------
    # ``rebuild_schedule_task``'s own tail re-triggers ``run_scheduling_task``
    # when ``pending_ops`` is non-empty (see ``workers/scheduling.py``
    # rebuild task body). So when we go this branch we MUST NOT call
    # ``_maybe_kick_run`` ourselves — that would enqueue a second drain
    # alongside the rebuild's own follow-up, doubling worker load for
    # nothing. Same reasoning applies to the unparseable-state branch.
    if raw_state is None:
        logger.warning("schedule.startup_recovery.state_missing", action="rebuild")
        rebuild_schedule_task.delay()
        return

    # --- Step 2: stale base_date → catch up via advance_day -----------------
    try:
        state = SchedulerState.from_json(raw_state)
    except Exception:
        # Corrupt state — treat like missing state and rebuild.
        logger.exception("schedule.startup_recovery.state_unparseable", action="rebuild")
        rebuild_schedule_task.delay()
        return

    missed_days = (today - state.base_date).days
    if missed_days < 0:
        # ``base_date`` is in the future relative to this replica's clock.
        # Likely cause: NTP drift / a peer replica with a fast clock wrote
        # state ahead of real time. We deliberately do NOT auto-advance
        # (would over-roll the calendar past today on the rest of the
        # fleet) and do NOT rebuild (state may be intentional and rebuild
        # would clobber the in-flight schedule). Just log a warning so
        # ops can investigate, then fall through to the orphan-pending
        # sweep — that branch is safe under either calendar direction.
        logger.warning(
            "schedule.startup_recovery.base_date_in_future",
            base_date=state.base_date.isoformat(),
            today=today.isoformat(),
            skew_days=-missed_days,
        )
    elif missed_days > 0:
        # Cap at the horizon — beyond that the state is so stale that
        # rebuild is cheaper than walking N advance_days, and most of the
        # work would be no-ops (everything past day-30 is empty).
        catchup_days = min(missed_days, HORIZON_DAYS)
        logger.warning(
            "schedule.startup_recovery.base_date_stale",
            base_date=state.base_date.isoformat(),
            today=today.isoformat(),
            missed_days=missed_days,
            catchup_days=catchup_days,
            capped=missed_days > HORIZON_DAYS,
        )
        if missed_days > HORIZON_DAYS:
            # Past horizon — rebuild is the right tool. Catchup advance_days
            # past HORIZON_DAYS would just be churn over empty trees.
            # Same reasoning as Step 1: rebuild self-retriggers run_task,
            # so return early without the kick.
            rebuild_schedule_task.delay()
            return
        for _ in range(catchup_days):
            advance_day_task.delay()

    # --- Step 3: orphan pending ops → re-trigger drain ----------------------
    _maybe_kick_run(rds, run_scheduling_task)


def _maybe_kick_run(rds: Redis, run_scheduling_task: object) -> None:
    """If queue has work but status isn't ``running``, dispatch a drain.

    Producer-side ``enqueue_compound`` only triggers the worker on the
    happy path — if a producer's ZADD survived but its ``.delay()`` was
    lost (e.g. the broker was unreachable for a moment, or the FastAPI
    process died between ZADD and delay), the compound sits forever.
    This sweep covers that gap on the next restart.
    """
    queued = cast("int", rds.zcard(PENDING_OPS_KEY))
    if queued <= 0:
        return

    status_doc = _read_status()
    state = (status_doc or {}).get("state")
    if state == "running":
        # Worker is already draining; don't pile on.
        logger.info(
            "schedule.startup_recovery.pending_ops_drain_in_progress",
            queued=queued,
        )
        return

    logger.warning(
        "schedule.startup_recovery.pending_ops_orphaned",
        queued=queued,
        status_state=state,
        action="kick_run",
    )
    run_scheduling_task.delay()  # type: ignore[attr-defined]
