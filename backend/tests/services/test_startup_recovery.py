"""Tests for ``app.services.startup_recovery``.

Recovery dispatches Celery tasks in response to three gap conditions
observed in Redis state:

* ``schedule:state`` missing or corrupt → rebuild
* ``state.base_date < today`` → advance_day catchup (or rebuild if past
  horizon)
* ``pending_ops`` non-empty AND status not running → kick a drain

Real Redis (from ``redis_client`` fixture) so the NX lock + state
read/write semantics match production. Celery dispatch is mocked because
there's no broker in-process; the assertion target is "the right task
was queued the right number of times".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from app.services.scheduling import (
    HORIZON_DAYS,
    PENDING_OPS_KEY,
    STATE_KEY,
    SchedulerState,
)
from app.services.startup_recovery import (
    _RECOVERY_FLAG_KEY,
    run_startup_recovery,
)
from redis import Redis


@pytest.fixture(autouse=True)
def _override_app_env_for_dispatch_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``APP_ENV=dev`` for every test in this file.

    ``run_startup_recovery`` early-returns when ``APP_ENV=="test"`` to keep
    the FastAPI lifespan fast under pytest's ``TestClient`` (every API test
    would otherwise re-dispatch Celery tasks on each ``client`` fixture
    instantiation). But this test file exists **specifically** to exercise
    the dispatcher itself, so we flip the env back to ``dev`` and bust the
    settings lru_cache so the new value takes effect.

    The full-suite `APP_ENV=test` env is restored by ``monkeypatch``'s
    automatic teardown after each test.
    """
    from app.core.config import get_settings

    monkeypatch.setenv("APP_ENV", "dev")
    get_settings.cache_clear()


def _patch_tasks(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Mock the three Celery tasks the recovery module dispatches.

    Patches the **import location inside the lazy import** — recovery
    pulls the tasks at call time via ``from app.workers.scheduling import
    ...`` so the canonical source-of-truth bindings are the ones tests
    need to swap.
    """
    rebuild = MagicMock()
    advance = MagicMock()
    run = MagicMock()
    monkeypatch.setattr("app.workers.scheduling.rebuild_schedule_task", rebuild)
    monkeypatch.setattr("app.workers.scheduling.advance_day_task", advance)
    monkeypatch.setattr("app.workers.scheduling.run_scheduling_task", run)
    return {"rebuild": rebuild, "advance": advance, "run": run}


def _today() -> datetime.date:
    # Must match the timezone used by ``startup_recovery._DAY_BOUNDARY_TZ``
    # — comparing against UTC here would make the catchup-counts tests
    # off-by-one inside the 00:00-08:00 Asia/Taipei window.
    return datetime.now(tz=ZoneInfo("Asia/Taipei")).date()


# ---------------------------------------------------------------------------
# state-missing → rebuild
# ---------------------------------------------------------------------------


def test_recovery_dispatches_rebuild_when_state_key_missing(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Fresh deploy / Redis flush leaves ``schedule:state`` unset. Recovery
    must rebuild from DB; otherwise the worker would silently fall back to
    an empty ``SchedulerState.initial(today)`` and forget existing
    scheduled orders' capacity load.
    """
    tasks = _patch_tasks(monkeypatch)
    redis_client.delete(STATE_KEY)

    run_startup_recovery()

    tasks["rebuild"].delay.assert_called_once()
    tasks["advance"].delay.assert_not_called()


def test_recovery_dispatches_rebuild_when_state_unparseable(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Garbled state (schema upgrade mid-deploy, manual surgery, partial
    Redis persistence) is functionally equivalent to missing state — we
    can't trust any of the trees / pq inside. Rebuild from the DB source
    of truth instead of trying to repair in place.
    """
    tasks = _patch_tasks(monkeypatch)
    redis_client.set(STATE_KEY, "{not valid json at all")

    run_startup_recovery()

    tasks["rebuild"].delay.assert_called_once()
    tasks["advance"].delay.assert_not_called()


# ---------------------------------------------------------------------------
# stale base_date → advance_day catchup
# ---------------------------------------------------------------------------


def test_recovery_catches_up_one_missed_day(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Server down for one midnight → base_date is yesterday. Queue exactly
    one advance_day_task; the celery worker will pick it up and bump the
    segment-tree day 1 to today.
    """
    tasks = _patch_tasks(monkeypatch)
    yesterday = _today() - timedelta(days=1)
    state = SchedulerState.initial(yesterday)
    redis_client.set(STATE_KEY, state.to_json())

    run_startup_recovery()

    assert tasks["advance"].delay.call_count == 1
    tasks["rebuild"].delay.assert_not_called()


def test_recovery_catches_up_multiple_missed_days(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Long weekend outage → 3 missed midnights → 3 advance_day_task
    dispatches. Each task internally serializes via the state-writer lock
    so queueing them all at once is safe.
    """
    tasks = _patch_tasks(monkeypatch)
    three_days_ago = _today() - timedelta(days=3)
    state = SchedulerState.initial(three_days_ago)
    redis_client.set(STATE_KEY, state.to_json())

    run_startup_recovery()

    assert tasks["advance"].delay.call_count == 3
    tasks["rebuild"].delay.assert_not_called()


def test_recovery_rebuilds_instead_of_catchup_when_past_horizon(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """If the gap exceeds the horizon, every day of "catchup" past day-30
    would just be advance_day churn over empty trees — rebuild is the
    right tool. Verifies the cap → rebuild handoff.
    """
    tasks = _patch_tasks(monkeypatch)
    far_past = _today() - timedelta(days=HORIZON_DAYS + 5)
    state = SchedulerState.initial(far_past)
    redis_client.set(STATE_KEY, state.to_json())

    run_startup_recovery()

    tasks["rebuild"].delay.assert_called_once()
    tasks["advance"].delay.assert_not_called()


def test_recovery_skips_advance_day_when_base_date_already_today(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Short restart that didn't cross midnight: base_date == today,
    nothing to catch up. Advance_day must not be queued (would over-shoot
    into tomorrow).
    """
    tasks = _patch_tasks(monkeypatch)
    state = SchedulerState.initial(_today())
    redis_client.set(STATE_KEY, state.to_json())

    run_startup_recovery()

    tasks["advance"].delay.assert_not_called()
    tasks["rebuild"].delay.assert_not_called()


def test_recovery_does_not_advance_when_base_date_is_in_future(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Clock-skew defensive path: a peer replica with a fast NTP clock
    wrote ``base_date = tomorrow`` to state. Recovery must NOT auto-advance
    (would over-roll the calendar past today across the fleet) and must
    NOT rebuild (state may be intentional). Should just log a warning
    and fall through to the orphan-pending sweep — which is safe under
    either calendar direction.
    """
    import logging

    tasks = _patch_tasks(monkeypatch)
    tomorrow = _today() + timedelta(days=1)
    state = SchedulerState.initial(tomorrow)
    redis_client.set(STATE_KEY, state.to_json())

    with caplog.at_level(logging.WARNING, logger="app.services.startup_recovery"):
        run_startup_recovery()

    tasks["advance"].delay.assert_not_called()
    tasks["rebuild"].delay.assert_not_called()
    # The warning must surface so ops can investigate NTP / replica drift.
    assert any(
        "base_date_in_future" in record.getMessage()
        or record.__dict__.get("event") == "schedule.startup_recovery.base_date_in_future"
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# orphan pending_ops → kick run_scheduling_task
# ---------------------------------------------------------------------------


def test_recovery_kicks_run_when_pending_ops_orphaned_and_status_idle(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Worker crashed between ZADD and ``.delay()`` (or broker was
    momentarily unreachable). State is current, queue has work, status
    says idle → no one will trigger a drain unless we do.
    """
    tasks = _patch_tasks(monkeypatch)
    redis_client.set(STATE_KEY, SchedulerState.initial(_today()).to_json())
    redis_client.zadd(PENDING_OPS_KEY, {'{"compound_id": "x"}': 1.0})
    redis_client.set("schedule:status", json.dumps({"state": "idle"}))

    run_startup_recovery()

    tasks["run"].delay.assert_called_once()


def test_recovery_does_not_kick_run_when_worker_already_running(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Don't pile a redundant task on top of an in-flight drain — the
    running worker will self-retrigger at end-of-drain if pending_ops is
    still non-empty (waiter-flag race fix in run_scheduling_task).
    """
    tasks = _patch_tasks(monkeypatch)
    redis_client.set(STATE_KEY, SchedulerState.initial(_today()).to_json())
    redis_client.zadd(PENDING_OPS_KEY, {'{"compound_id": "x"}': 1.0})
    redis_client.set("schedule:status", json.dumps({"state": "running"}))

    run_startup_recovery()

    tasks["run"].delay.assert_not_called()


def test_recovery_does_not_kick_run_when_queue_empty(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """No pending compounds, no work to do — the kick path must not fire
    on every restart. Common case: clean shutdown then clean restart.
    """
    tasks = _patch_tasks(monkeypatch)
    redis_client.set(STATE_KEY, SchedulerState.initial(_today()).to_json())
    redis_client.delete(PENDING_OPS_KEY)
    redis_client.set("schedule:status", json.dumps({"state": "idle"}))

    run_startup_recovery()

    tasks["run"].delay.assert_not_called()


# ---------------------------------------------------------------------------
# Multi-replica mutex
# ---------------------------------------------------------------------------


def test_recovery_skips_when_another_replica_holds_flag(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Multi-replica deploy must not have every replica queueing recovery
    tasks (advance_day catchup would over-advance state). NX flag held by
    a peer → this replica logs + bails without touching the queue.
    """
    tasks = _patch_tasks(monkeypatch)
    # Pretend another replica got there first.
    redis_client.set(_RECOVERY_FLAG_KEY, "1", ex=60)
    # Set up conditions that WOULD trigger recovery if the flag wasn't
    # held — proves the mutex is what's blocking, not absence of work.
    yesterday = _today() - timedelta(days=1)
    redis_client.set(STATE_KEY, SchedulerState.initial(yesterday).to_json())

    run_startup_recovery()

    tasks["advance"].delay.assert_not_called()
    tasks["rebuild"].delay.assert_not_called()


def test_recovery_releases_flag_after_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
) -> None:
    """Flag must be DEL'd at the end of dispatch (not just left to TTL),
    otherwise back-to-back restarts within 60s would skip recovery on the
    second one even though it's the only replica.
    """
    _patch_tasks(monkeypatch)
    redis_client.set(STATE_KEY, SchedulerState.initial(_today()).to_json())

    run_startup_recovery()

    assert redis_client.get(_RECOVERY_FLAG_KEY) is None


# ---------------------------------------------------------------------------
# Orphan-lock cleanup
# ---------------------------------------------------------------------------
#
# ``is_processing_locked=True`` is set by the producer right before
# enqueueing a compound and cleared by the worker
# (``compound_finalize.perform_compound_db_action``) after accept/reject.
# If the worker crashes in between, the lock survives and silently
# blocks every future PATCH / DELETE / cancel on that row until manual
# DB surgery. ``_clear_orphan_locks`` is the startup sweep that closes
# this gap.


class _NonClosingSession:
    """Wraps a Session so ``.close()`` is a no-op; lets startup_recovery
    open + close its own session while still landing writes inside the
    per-test SAVEPOINT that the outer ``db_session`` fixture rolls back.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _patch_recovery_sessionlocal(monkeypatch, db_session) -> None:
    """Route ``startup_recovery.SessionLocal()`` to the per-test session."""
    monkeypatch.setattr(
        "app.services.startup_recovery.SessionLocal",
        lambda: _NonClosingSession(db_session),
    )


def _seed_user(db, *, username: str):
    """Lightweight user seed for the lock-cleanup tests."""
    import bcrypt
    from app.models.user import User, UserRole

    user = User(
        username=username,
        email=f"{username}@test.internal",
        password_hash=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        role=UserRole.scheduler,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _refetch_order(db, order_id):
    """Re-SELECT the order through a fresh expire — the recovery code commits
    via a wrapped Session, which can detach the pytest fixture's ORM-bound
    instance. Querying by id is the cleanest way to read post-commit state.
    """
    from app.models.order import Order
    from sqlalchemy import select

    db.expire_all()
    return db.scalar(select(Order).where(Order.id == order_id))


def _seed_order(db, *, created_by, order_number: str, is_locked: bool, is_deleted: bool = False):
    from datetime import date as _date

    from app.models.order import Order, OrderStatus

    order = Order(
        order_number=order_number,
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=_date(2026, 6, 15),
        created_by=created_by,
        status=OrderStatus.pending,
        is_processing_locked=is_locked,
        is_deleted=is_deleted,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_recovery_clears_orphan_lock_when_compound_not_in_queue(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    db_session,
) -> None:
    """Producer committed ``is_processing_locked=True`` and crashed before /
    during ``enqueue_compound``. Compound isn't in pending_ops. The lock
    must be cleared on the next startup or the row is hosed forever.
    """
    _patch_tasks(monkeypatch)
    _patch_recovery_sessionlocal(monkeypatch, db_session)
    redis_client.set(STATE_KEY, SchedulerState.initial(_today()).to_json())
    # Empty pending_ops queue — no compound covers any order.
    redis_client.delete(PENDING_OPS_KEY)

    user = _seed_user(db_session, username="recover-orphan-1")
    orphan = _seed_order(
        db_session,
        created_by=user.id,
        order_number="ORD-ORPHAN-LOCK",
        is_locked=True,
    )

    orphan_id = orphan.id
    run_startup_recovery()

    fetched = _refetch_order(db_session, orphan_id)
    assert fetched.is_processing_locked is False, "orphan lock must be cleared by startup recovery"


def test_recovery_preserves_lock_when_compound_still_in_queue(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    db_session,
) -> None:
    """A row whose compound is genuinely in pending_ops must NOT have its
    lock cleared — the worker will handle it on the next drain. Clearing
    here would race with the worker (second user op slips in before the
    worker's accept commits its own state writes).
    """
    _patch_tasks(monkeypatch)
    _patch_recovery_sessionlocal(monkeypatch, db_session)
    redis_client.set(STATE_KEY, SchedulerState.initial(_today()).to_json())

    user = _seed_user(db_session, username="recover-orphan-2")
    in_flight = _seed_order(
        db_session,
        created_by=user.id,
        order_number="ORD-INFLIGHT-LOCK",
        is_locked=True,
    )

    # Put a compound in the queue that references this order.
    compound = {
        "compound_id": "11111111-1111-1111-1111-111111111111",
        "group": "grow",
        "op_count": 1,
        "requested_by": str(user.id),
        "ops": [
            {
                "op": "add",
                "order_id": str(in_flight.id),
                "order_number": in_flight.order_number,
                "wafer_quantity": 100,
                "deadline": "2026-06-15",
            }
        ],
    }
    redis_client.zadd(PENDING_OPS_KEY, {json.dumps(compound): 0.0})

    in_flight_id = in_flight.id
    run_startup_recovery()

    fetched = _refetch_order(db_session, in_flight_id)
    assert fetched.is_processing_locked is True, (
        "in-flight lock must NOT be cleared while its compound is still queued"
    )


def test_recovery_skips_soft_deleted_locked_rows(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    db_session,
) -> None:
    """Soft-deleted rows are filtered out of the orphan-lock scan via the
    ``is_deleted=False`` filter in the SELECT — they can't be opened by
    a user op anyway (``get_by_id_for_update`` has the same filter), so
    clearing their lock is pointless work.

    The contract under test: ``_clear_orphan_locks`` must NOT clear
    ``is_processing_locked`` on a soft-deleted row even when its
    order_id isn't in the in-flight set. Pre-strengthening this only
    asserted "no crash"; now we explicitly verify lock state is
    preserved — if a future refactor drops the ``is_deleted`` filter
    from the SELECT, this test fails.
    """
    _patch_tasks(monkeypatch)
    _patch_recovery_sessionlocal(monkeypatch, db_session)
    redis_client.set(STATE_KEY, SchedulerState.initial(_today()).to_json())
    redis_client.delete(PENDING_OPS_KEY)

    user = _seed_user(db_session, username="recover-orphan-3")
    deleted = _seed_order(
        db_session,
        created_by=user.id,
        order_number="ORD-SOFTDEL-LOCK",
        is_locked=True,
        is_deleted=True,
    )

    deleted_id = deleted.id
    run_startup_recovery()

    fetched = _refetch_order(db_session, deleted_id)
    assert fetched.is_deleted is True, "soft-delete flag must be preserved"
    assert fetched.is_processing_locked is True, (
        "soft-deleted rows must be SKIPPED by orphan-lock sweep — lock state must NOT be touched"
    )
