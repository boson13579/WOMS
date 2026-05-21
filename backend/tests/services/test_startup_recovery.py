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
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

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
    return datetime.now(tz=UTC).date()


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
