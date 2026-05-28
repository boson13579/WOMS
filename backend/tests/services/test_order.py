"""Targeted tests for ``app.services.order`` paths touched by the scheduler.

Per RULES.md §5 (TDD), every functional change ships with a regression test.

Scope is intentionally narrow: ``apply_schedule`` behaviour, concurrent PATCH
isolation (StaleDataError + SAVEPOINT), and DB-write correctness. Broader
``services/order`` coverage lives elsewhere.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from unittest.mock import MagicMock

import bcrypt
import pytest
from app.models.audit_log import AuditLog
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from app.schemas.order import CreateOrderRequest, UpdateOrderRequest
from app.services import order as order_service
from app.services.scheduling import ScheduledResult
from sqlalchemy import select
from sqlalchemy.orm import Session


@pytest.fixture
def mock_enqueue(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Capture ``enqueue_compound`` calls made by the order service.

    Order CRUD now pushes compounds to the scheduler queue after every
    create / update / delete; tests that don't need a live Redis bind the
    helper to a mock and inspect what compound the service built.
    """
    mock = MagicMock()
    monkeypatch.setattr("app.services.order.enqueue_compound", mock)
    return mock


def _make_user(db: Session, *, username: str) -> User:
    user = User(
        username=username,
        email=f"{username}@test.internal",
        password_hash=bcrypt.hashpw(b"password123", bcrypt.gensalt()).decode(),
        role=UserRole.scheduler,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_order(
    db: Session,
    *,
    creator_id: uuid.UUID,
    order_number: str,
    deadline: date,
    quantity: int = 100,
    assigned_to: uuid.UUID | None = None,
) -> Order:
    order = Order(
        order_number=order_number,
        customer_name="ACME",
        wafer_quantity=quantity,
        requested_delivery_date=deadline,
        created_by=creator_id,
        assigned_to=assigned_to,
        status=OrderStatus.pending,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_apply_schedule_returns_correct_applied_count(db_session: Session) -> None:
    """apply_schedule returns the count of orders whose dates were written.

    Multi-day assignments for one order count as a single applied order (the
    date fold collapses them to earliest/latest). Single-day for another order
    counts as one more. Total: 2.
    """
    creator = _make_user(db_session, username="apply-sched-user-1")
    order_a = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-AUDIT-A",
        deadline=date(2026, 5, 20),
    )
    order_b = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-AUDIT-B",
        deadline=date(2026, 5, 22),
    )

    scheduled = [
        ScheduledResult(order_id=order_a.id, scheduled_date=date(2026, 5, 12), quantity=60),
        ScheduledResult(order_id=order_a.id, scheduled_date=date(2026, 5, 13), quantity=40),
        ScheduledResult(order_id=order_b.id, scheduled_date=date(2026, 5, 14), quantity=100),
    ]

    applied = order_service.apply_schedule(db_session, scheduled)
    assert applied == 2

    db_session.refresh(order_a)
    db_session.refresh(order_b)
    assert order_a.scheduled_production_date == date(2026, 5, 12)
    assert order_a.expected_delivery_date == date(2026, 5, 13)
    assert order_b.scheduled_production_date == date(2026, 5, 14)
    assert order_b.expected_delivery_date == date(2026, 5, 14)


def test_apply_schedule_writes_daily_capacity_snapshot(db_session: Session) -> None:
    """The per-day capacity-usage snapshot must be written alongside per-order
    daily_breakdown rows, in the same transaction.

    Aggregation rule: for each scheduled date, ``used_quantity`` is the sum
    of every order's quantity scheduled on that date. The dashboard endpoint
    relies on this single source of truth — recomputing on-the-fly would
    require JSONB aggregation, which is why we cache it eagerly.
    """
    from app.models.schedule_daily_capacity import ScheduleDailyCapacity

    creator = _make_user(db_session, username="apply-sched-snapshot")
    order_a = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-CAP-A",
        deadline=date(2026, 5, 20),
    )
    order_b = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-CAP-B",
        deadline=date(2026, 5, 21),
    )

    scheduled = [
        # Two orders share day 5/12 → 700 wafers; only A is on 5/13;
        # only B is on 5/14. (Per-day ``ScheduledResult.quantity`` is
        # independent of ``Order.wafer_quantity``'s [25, 2500] check
        # constraint — values here just need to be plausible aggregates.)
        ScheduledResult(order_id=order_a.id, scheduled_date=date(2026, 5, 12), quantity=400),
        ScheduledResult(order_id=order_a.id, scheduled_date=date(2026, 5, 13), quantity=200),
        ScheduledResult(order_id=order_b.id, scheduled_date=date(2026, 5, 12), quantity=300),
        ScheduledResult(order_id=order_b.id, scheduled_date=date(2026, 5, 14), quantity=100),
    ]
    order_service.apply_schedule(db_session, scheduled)

    rows = list(
        db_session.scalars(
            select(ScheduleDailyCapacity).order_by(ScheduleDailyCapacity.date.asc())
        ).all()
    )

    by_date = {row.date: row.used_quantity for row in rows}
    # 5/12 = a(400) + b(300) = 700
    assert by_date == {
        date(2026, 5, 12): 700,
        date(2026, 5, 13): 200,
        date(2026, 5, 14): 100,
    }


def test_apply_schedule_snapshot_replaces_previous_run(db_session: Session) -> None:
    """A second ``apply_schedule`` call must replace the snapshot wholesale —
    no rows from the prior run can leak through. Guards against the bug where
    we'd ``upsert`` instead of truncate-and-insert and leave stale dates
    behind after a schedule change (e.g. an order's deadline moved earlier).
    """
    from app.models.schedule_daily_capacity import ScheduleDailyCapacity

    creator = _make_user(db_session, username="apply-sched-snapshot-replace")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-REPL",
        deadline=date(2026, 6, 1),
    )

    # First run: order goes on 5/15.
    order_service.apply_schedule(
        db_session,
        [ScheduledResult(order_id=order.id, scheduled_date=date(2026, 5, 15), quantity=100)],
    )
    # Sanity: snapshot has 5/15 only.
    rows = list(db_session.scalars(select(ScheduleDailyCapacity)).all())
    assert {r.date for r in rows} == {date(2026, 5, 15)}

    # Second run: order moves to 5/16 entirely. 5/15 must vanish.
    order_service.apply_schedule(
        db_session,
        [ScheduledResult(order_id=order.id, scheduled_date=date(2026, 5, 16), quantity=100)],
    )

    rows = list(db_session.scalars(select(ScheduleDailyCapacity)).all())
    assert {r.date for r in rows} == {date(2026, 5, 16)}, (
        "Truncate-and-insert contract broken: stale 5/15 row leaked through"
    )


def test_apply_schedule_preserves_today_snapshot_row(db_session: Session) -> None:
    """``preserve_capacity_for=today`` keeps that day's
    ``schedule_daily_capacity`` row across the truncate-and-insert.

    Used by ``materialize_schedule_task`` / ``rebuild_schedule_task``
    whose ``compute_schedule`` output only covers future days
    (``base_date + 1`` and later) — without this preserve, today's
    row (= in_production wafer量 written by ``advance_day_task``) gets
    wiped on every subsequent materialize and ``GET
    /schedule/capacity-usage`` shows 0 used for today, even though
    13+ wafers are actively being produced.

    Regression case from production: after 5/27→5/28 advance,
    advance_day_task wrote schedule_daily_capacity[5/28]=10000. Next
    compound landed, materializer triggered, the old code wiped the
    5/28 row → frontend "today used = 0".
    """
    from app.models.schedule_daily_capacity import ScheduleDailyCapacity

    creator = _make_user(db_session, username="apply-sched-preserve-today")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-PRESERVE",
        deadline=date(2026, 6, 1),
    )
    today = date(2026, 5, 28)

    # Simulate advance_day_task's write: today's in_production wafer量.
    db_session.add(ScheduleDailyCapacity(date=today, used_quantity=9_500))
    db_session.commit()

    # Materializer's next pass: ``compute_schedule`` only sees future
    # days, no entries on ``today``. Pre-fix this would wipe today's
    # row; with ``preserve_capacity_for=today`` it survives.
    order_service.apply_schedule(
        db_session,
        [
            ScheduledResult(
                order_id=order.id,
                scheduled_date=date(2026, 5, 29),
                quantity=300,
            ),
        ],
        preserve_capacity_for=today,
    )

    rows = {
        r.date: r.used_quantity for r in db_session.scalars(select(ScheduleDailyCapacity)).all()
    }
    assert rows == {today: 9_500, date(2026, 5, 29): 300}, (
        "preserve_capacity_for=today must keep today's snapshot row intact "
        "while still replacing future-day rows."
    )


def test_apply_schedule_preserve_capacity_ignores_today_entry_in_new_set(
    db_session: Session,
) -> None:
    """When the caller passes a ``scheduled_date == preserve_capacity_for``
    entry, the preserved existing row wins — the conflicting "new" entry
    is dropped so it can't double-write or override.

    Defends against a caller accidentally including a today-row entry
    in ``scheduled_results`` while also asking to preserve today: the
    semantics stay "today's row is owned by advance_day_task only".
    """
    from app.models.schedule_daily_capacity import ScheduleDailyCapacity

    creator = _make_user(db_session, username="apply-sched-preserve-collide")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-COLLIDE",
        deadline=date(2026, 6, 1),
    )
    today = date(2026, 5, 28)

    db_session.add(ScheduleDailyCapacity(date=today, used_quantity=7_000))
    db_session.commit()

    order_service.apply_schedule(
        db_session,
        [
            # Bad caller: passes a same-day entry alongside preserve.
            ScheduledResult(
                order_id=order.id,
                scheduled_date=today,
                quantity=999,
            ),
        ],
        preserve_capacity_for=today,
    )

    rows = {
        r.date: r.used_quantity for r in db_session.scalars(select(ScheduleDailyCapacity)).all()
    }
    # Preserved row survives; the 999 entry on today is dropped.
    assert rows == {today: 7_000}


def test_apply_schedule_with_empty_list_clears_snapshot(db_session: Session) -> None:
    """Materialize-with-no-orders (entire schedule cleared) must wipe the
    snapshot too — leaving stale rows would have the dashboard show wafers
    committed to days that no longer have any orders scheduled.
    """
    from app.models.schedule_daily_capacity import ScheduleDailyCapacity

    creator = _make_user(db_session, username="apply-sched-snapshot-empty")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-EMPTY-SNAP",
        deadline=date(2026, 6, 5),
    )
    # Prime the snapshot.
    order_service.apply_schedule(
        db_session,
        [ScheduledResult(order_id=order.id, scheduled_date=date(2026, 5, 20), quantity=50)],
    )
    assert db_session.scalars(select(ScheduleDailyCapacity)).all()

    # Now clear.
    order_service.apply_schedule(db_session, [])
    assert not db_session.scalars(select(ScheduleDailyCapacity)).all()


def test_apply_schedule_notifies_when_date_changes_from_null_to_set(
    db_session: Session,
) -> None:
    """Promoting a pending order to scheduled (null → date) must create
    one ``order_status_changed`` notification for ``order.created_by``.
    First-time scheduling is exactly the case users want to know about.
    """
    from app.models.notification import Notification

    creator = _make_user(db_session, username="notif-null-to-date")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-NOTIF-NEW",
        deadline=date(2026, 7, 1),
    )
    # Sanity: order starts unscheduled (no scheduled_production_date).
    assert order.scheduled_production_date is None

    order_service.apply_schedule(
        db_session,
        [ScheduledResult(order_id=order.id, scheduled_date=date(2026, 6, 15), quantity=100)],
    )

    notifs = list(
        db_session.scalars(
            select(Notification)
            .where(Notification.user_id == creator.id)
            .where(Notification.order_id == order.id)
        ).all()
    )
    assert len(notifs) == 1
    assert notifs[0].type == "order_status_changed"
    # Message should carry the new date so the notification-center row is
    # self-contained (no click-through to find out which date).
    assert "2026-06-15" in notifs[0].message
    assert order.order_number in notifs[0].message


def test_apply_schedule_notifies_when_date_changes_from_a_to_b(
    db_session: Session,
) -> None:
    """Rescheduling an already-scheduled order (A → B) must create one
    notification on the SECOND apply_schedule run. The first run is the
    null→A promotion (also notifies); we filter to notifications created
    after that to isolate the A→B case.
    """
    from app.models.notification import Notification

    creator = _make_user(db_session, username="notif-a-to-b")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-NOTIF-RESCH",
        deadline=date(2026, 7, 1),
    )

    # First apply: null → 6/15. Generates one "promoted" notification.
    order_service.apply_schedule(
        db_session,
        [ScheduledResult(order_id=order.id, scheduled_date=date(2026, 6, 15), quantity=100)],
    )
    first_run_count = len(
        list(
            db_session.scalars(select(Notification).where(Notification.order_id == order.id)).all()
        )
    )
    assert first_run_count == 1

    # Second apply: 6/15 → 6/20. Must generate exactly one more notification
    # with both old and new dates in the message.
    order_service.apply_schedule(
        db_session,
        [ScheduledResult(order_id=order.id, scheduled_date=date(2026, 6, 20), quantity=100)],
    )

    all_notifs = list(
        db_session.scalars(
            select(Notification)
            .where(Notification.order_id == order.id)
            .order_by(Notification.created_at.asc())
        ).all()
    )
    assert len(all_notifs) == 2
    reschedule_msg = all_notifs[1].message
    assert "2026-06-15" in reschedule_msg, "Old date missing from reschedule notification"
    assert "2026-06-20" in reschedule_msg, "New date missing from reschedule notification"


def test_apply_schedule_does_not_notify_when_date_unchanged(
    db_session: Session,
) -> None:
    """Idempotent re-materialize (same scheduled_production_date as before)
    must NOT create a duplicate notification. This is the core anti-spam
    contract — every accepted compound triggers materialize_schedule_task,
    most don't actually move any order's start date, so without this guard
    the notification center floods with NxN redundant entries.
    """
    from app.models.notification import Notification

    creator = _make_user(db_session, username="notif-unchanged")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-NOTIF-NOOP",
        deadline=date(2026, 7, 1),
    )

    # First apply: null → 6/15. One "promoted" notification.
    order_service.apply_schedule(
        db_session,
        [ScheduledResult(order_id=order.id, scheduled_date=date(2026, 6, 15), quantity=100)],
    )

    # Second apply: still 6/15 (same date, possibly different per-day split).
    # Must not create a second notification.
    order_service.apply_schedule(
        db_session,
        [
            ScheduledResult(order_id=order.id, scheduled_date=date(2026, 6, 15), quantity=60),
            ScheduledResult(order_id=order.id, scheduled_date=date(2026, 6, 16), quantity=40),
        ],
    )

    notifs = list(
        db_session.scalars(select(Notification).where(Notification.order_id == order.id)).all()
    )
    assert len(notifs) == 1, (
        f"expected 1 notification (only the initial null→6/15 promotion), got {len(notifs)}: "
        f"{[n.message for n in notifs]}"
    )


def test_apply_schedule_notifies_both_creator_and_assignee(db_session: Session) -> None:
    """When ``assigned_to`` is set AND different from ``created_by``, both
    users get the ``order_status_changed`` notification. Assignee owns
    day-to-day production handling, so they need to know when the start
    date moves; creator usually owns the customer relationship, so they
    also need to know — both are legitimate stakeholders.
    """
    from app.models.notification import Notification

    creator = _make_user(db_session, username="notif-creator")
    assignee = _make_user(db_session, username="notif-assignee")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        assigned_to=assignee.id,
        order_number="ORD-NOTIF-DUAL",
        deadline=date(2026, 7, 1),
    )

    order_service.apply_schedule(
        db_session,
        [ScheduledResult(order_id=order.id, scheduled_date=date(2026, 6, 15), quantity=100)],
    )

    creator_notifs = list(
        db_session.scalars(
            select(Notification)
            .where(Notification.user_id == creator.id)
            .where(Notification.order_id == order.id)
        ).all()
    )
    assignee_notifs = list(
        db_session.scalars(
            select(Notification)
            .where(Notification.user_id == assignee.id)
            .where(Notification.order_id == order.id)
        ).all()
    )
    assert len(creator_notifs) == 1, "creator missed the notification"
    assert len(assignee_notifs) == 1, "assignee missed the notification"
    # Same message content for both — single source of truth for the
    # status-changed event.
    assert creator_notifs[0].message == assignee_notifs[0].message


def test_apply_schedule_dedupes_when_creator_is_also_assignee(
    db_session: Session,
) -> None:
    """When the order's creator self-assigned (``created_by == assigned_to``),
    they must get exactly ONE notification, not two. Otherwise self-assigned
    orders become twice as noisy as the rest for no reason.
    """
    from app.models.notification import Notification

    creator = _make_user(db_session, username="notif-self-assigned")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        assigned_to=creator.id,  # self-assigned
        order_number="ORD-NOTIF-SELF",
        deadline=date(2026, 7, 1),
    )

    order_service.apply_schedule(
        db_session,
        [ScheduledResult(order_id=order.id, scheduled_date=date(2026, 6, 15), quantity=100)],
    )

    notifs = list(
        db_session.scalars(
            select(Notification)
            .where(Notification.user_id == creator.id)
            .where(Notification.order_id == order.id)
        ).all()
    )
    assert len(notifs) == 1, (
        f"self-assigned order should produce exactly 1 notification, got {len(notifs)}"
    )


def test_get_capacity_usage_returns_continuous_horizon(db_session: Session) -> None:
    """``get_capacity_usage`` must fill the full 30-day window even if the
    snapshot only has a few dates populated. Dates without snapshot rows come
    back as ``used=0, remaining=daily_capacity`` so the dashboard renders a
    contiguous timeline.
    """
    creator = _make_user(db_session, username="capacity-usage-fill")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-USAGE-FILL",
        deadline=date(2026, 6, 30),
    )
    order_service.apply_schedule(
        db_session,
        [
            ScheduledResult(order_id=order.id, scheduled_date=date(2026, 5, 22), quantity=3_500),
            ScheduledResult(order_id=order.id, scheduled_date=date(2026, 5, 25), quantity=500),
        ],
    )

    base, capacity, entries = order_service.get_capacity_usage(
        db_session,
        base_date=date(2026, 5, 21),
        daily_capacity=10_000,
        horizon_days=30,
    )

    assert base == date(2026, 5, 21)
    assert capacity == 10_000
    assert len(entries) == 30
    # Day 1 (5/21): unused (no snapshot row for this date).
    assert (entries[0].date, entries[0].used, entries[0].remaining) == (
        date(2026, 5, 21),
        0,
        10_000,
    )
    # Day 2 (5/22): 3500 used.
    assert (entries[1].date, entries[1].used, entries[1].remaining) == (
        date(2026, 5, 22),
        3_500,
        6_500,
    )
    # Day 5 (5/25): 500 used.
    day_5_25 = next(e for e in entries if e.date == date(2026, 5, 25))
    assert (day_5_25.used, day_5_25.remaining) == (500, 9_500)
    # Last day (5/21 + 29 = 6/19): unused, no snapshot row.
    assert (entries[-1].date, entries[-1].used, entries[-1].remaining) == (
        date(2026, 6, 19),
        0,
        10_000,
    )


def test_get_capacity_usage_ignores_dates_outside_window(db_session: Session) -> None:
    """Snapshot rows for dates before ``base_date`` or past the horizon end
    must be excluded from the response — those represent yesterday's plan
    that the next advance_day will wipe, and we don't want them surfacing
    as "weirdly old" entries on the dashboard.
    """
    creator = _make_user(db_session, username="capacity-usage-window")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-USAGE-WIN",
        deadline=date(2026, 8, 30),
    )
    # Seed snapshot rows spanning before-window, in-window, and after-window.
    order_service.apply_schedule(
        db_session,
        [
            ScheduledResult(order_id=order.id, scheduled_date=date(2026, 5, 1), quantity=1_000),
            ScheduledResult(order_id=order.id, scheduled_date=date(2026, 6, 1), quantity=2_000),
            ScheduledResult(order_id=order.id, scheduled_date=date(2026, 7, 1), quantity=3_000),
        ],
    )

    _, _, entries = order_service.get_capacity_usage(
        db_session,
        base_date=date(2026, 5, 20),
        daily_capacity=10_000,
        horizon_days=30,
    )
    used_dates = {e.date for e in entries if e.used > 0}
    # 5/20 + 29 = 6/18, so 6/1 is in-window; 5/1 and 7/1 are not.
    assert used_dates == {date(2026, 6, 1)}


def test_apply_schedule_persists_daily_breakdown_column(db_session: Session) -> None:
    """``apply_schedule`` must write the per-day quantity split into
    ``orders.daily_breakdown`` (JSONB) alongside the earliest/latest date
    summary. The materializer flow promises that ``GET /schedule/result``
    can read the breakdown straight from the DB column — that contract
    only holds if apply_schedule writes the column in the right shape:
    ``[{"date": "...", "quantity": N}, ...]`` sorted by date.
    """
    creator = _make_user(db_session, username="apply-sched-breakdown")
    multi = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-BD-MULTI",
        deadline=date(2026, 5, 25),
    )
    single = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-BD-SINGLE",
        deadline=date(2026, 5, 26),
    )

    # Multi-day order: deliberately pass days out of order so we can also
    # assert the service sorts the stored JSON by date.
    scheduled = [
        ScheduledResult(order_id=multi.id, scheduled_date=date(2026, 5, 13), quantity=4_000),
        ScheduledResult(order_id=multi.id, scheduled_date=date(2026, 5, 12), quantity=10_000),
        ScheduledResult(order_id=single.id, scheduled_date=date(2026, 5, 14), quantity=500),
    ]
    order_service.apply_schedule(db_session, scheduled)

    db_session.refresh(multi)
    db_session.refresh(single)

    assert multi.daily_breakdown == [
        {"date": "2026-05-12", "quantity": 10_000},
        {"date": "2026-05-13", "quantity": 4_000},
    ]
    assert single.daily_breakdown == [
        {"date": "2026-05-14", "quantity": 500},
    ]
    # Summary dates remain correct alongside the breakdown.
    assert multi.scheduled_production_date == date(2026, 5, 12)
    assert multi.expected_delivery_date == date(2026, 5, 13)


def test_apply_schedule_with_no_results_writes_no_audit_rows(db_session: Session) -> None:
    """An empty ``scheduled`` list still wipes prior dates (clear-then-write
    contract) but must NOT manufacture audit rows. Guards against a regression
    where an off-by-one writes one row per cleared order instead of per
    applied order.
    """
    creator = _make_user(db_session, username="apply-sched-user-2")
    _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-EMPTY",
        deadline=date(2026, 5, 30),
    )

    applied = order_service.apply_schedule(db_session, [])
    assert applied == 0

    rows = db_session.scalars(select(AuditLog).where(AuditLog.action == "order.scheduled")).all()
    assert list(rows) == []


def test_apply_schedule_clears_stale_pin_columns_on_orders_no_longer_in_state(
    db_session: Session,
) -> None:
    """If a previously-pinned order is no longer in the new schedule (e.g.
    advance_day removed it because its pin day was today and it's now in
    production), the DB's ``is_pinned`` / ``pinned_production_date`` must
    be wiped — they reflect "currently pinned", not "was once pinned".

    Without the wipe, that order would stay ``is_pinned=true`` forever
    pointing at a date that's already past, confusing the frontend lock UI
    and showing wrong info in the orders list.
    """
    creator = _make_user(db_session, username="apply-sched-user-3")
    # Seed a stale order: it was pinned, scheduled, etc., but the new
    # apply_schedule run no longer includes it.
    stale = Order(
        order_number="ORD-STALE-PIN",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 5, 25),
        created_by=creator.id,
        status=OrderStatus.scheduled,
        scheduled_production_date=date(2026, 5, 10),
        expected_delivery_date=date(2026, 5, 10),
        is_pinned=True,
        pinned_production_date=date(2026, 5, 10),
        daily_breakdown=[{"date": "2026-05-10", "quantity": 100}],
    )
    db_session.add(stale)
    db_session.commit()

    # New apply_schedule run with EMPTY scheduled list — the stale order is
    # no longer part of the schedule.
    order_service.apply_schedule(db_session, [])

    db_session.refresh(stale)
    # Dates + pin columns + breakdown all wiped; the stale row no longer
    # reads as pinned or scheduled in any way.
    assert stale.scheduled_production_date is None
    assert stale.expected_delivery_date is None
    assert stale.daily_breakdown is None
    assert stale.is_pinned is False
    assert stale.pinned_production_date is None


def test_apply_schedule_skips_in_production_rows_entirely(db_session: Session) -> None:
    """A row whose status is already ``in_production`` MUST NOT have its
    schedule columns (``scheduled_production_date`` / ``expected_delivery
    _date`` / ``daily_breakdown``) touched by ``apply_schedule``.

    Why all-or-nothing skip rather than "preserve status only":
    ``advance_day_task`` writes the full multi-day ``daily_breakdown``
    for the locked-in row (today's portion + any carry into following
    days) when promoting it to ``in_production``. Subsequent
    materializer passes only see the REMAINING work in the in-memory
    state — so if ``set_schedule_dates`` ran on an in_production row
    it would overwrite the multi-day breakdown to a single-day one,
    making the boundary order vanish from the calendar's "today" cell
    and shrinking ``schedule_daily_capacity`` to exclude today's
    in_production wafers.

    Scenario: boundary order produced 1,500 wafers yesterday (5/12)
    and the remaining 500 today (5/13). DB reflects the full
    breakdown. ``materialize_schedule_task`` triggers
    ``apply_schedule`` with the state-derived view (only "5/13, 500"
    remaining). The schedule columns MUST stay frozen.
    """
    creator = _make_user(db_session, username="apply-sched-preserve-status")
    boundary = Order(
        order_number="ORD-BOUNDARY",
        customer_name="ACME",
        # Quantity respects the ck_orders_wafer_quantity check constraint
        # (25..2500). Picked so a hypothetical "today portion + tomorrow
        # portion" split is illustrative without violating the CHECK.
        wafer_quantity=2_000,
        requested_delivery_date=date(2026, 5, 14),
        created_by=creator.id,
        status=OrderStatus.in_production,
        scheduled_production_date=date(2026, 5, 12),
        expected_delivery_date=date(2026, 5, 13),
        daily_breakdown=[
            {"date": "2026-05-12", "quantity": 1_500},
            {"date": "2026-05-13", "quantity": 500},
        ],
    )
    db_session.add(boundary)
    db_session.commit()

    # Materializer's next pass: only tomorrow's portion remains in state
    # (today's 1,500 is already produced). Re-materialize for tomorrow.
    order_service.apply_schedule(
        db_session,
        [
            ScheduledResult(
                order_id=boundary.id,
                scheduled_date=date(2026, 5, 13),
                quantity=500,
            ),
        ],
    )

    db_session.refresh(boundary)
    # All schedule columns frozen — the multi-day breakdown that
    # advance_day_task wrote when promoting this row to in_production
    # must survive subsequent materializer passes.
    assert boundary.status == OrderStatus.in_production
    assert boundary.scheduled_production_date == date(2026, 5, 12)
    assert boundary.expected_delivery_date == date(2026, 5, 13)
    assert boundary.daily_breakdown == [
        {"date": "2026-05-12", "quantity": 1_500},
        {"date": "2026-05-13", "quantity": 500},
    ]


def test_apply_schedule_preserves_cancelled_status(db_session: Session) -> None:
    """A row whose ``status='cancelled'`` MUST NOT be demoted back to
    ``scheduled`` when ``apply_schedule`` runs on a (possibly stale)
    schedule that still references this order.

    Race this protects against (observed in production via
    consistency_check.py): a previous materializer reads
    ``schedule:state`` snapshot where order X is still in pq, then the
    worker's next batch accepts a cancel compound for X (pq_remove +
    DB ``status=cancelled``). The previous materializer's
    ``apply_schedule`` loop then reaches X with the stale snapshot's
    schedule entry — pre-fix, ``set_schedule_dates`` would
    unconditionally write ``status=scheduled``, silently un-cancelling
    the row. Same shape as the ``in_production`` regression above —
    materializer must respect terminal statuses set by other writers.
    """
    creator = _make_user(db_session, username="apply-sched-preserve-cancelled")
    cancelled = Order(
        order_number="ORD-CANCELLED-RACE",
        customer_name="ACME",
        wafer_quantity=200,
        requested_delivery_date=date(2026, 5, 14),
        created_by=creator.id,
        status=OrderStatus.cancelled,
        scheduled_production_date=date(2026, 5, 12),
        expected_delivery_date=date(2026, 5, 12),
        daily_breakdown=[{"date": "2026-05-12", "quantity": 200}],
    )
    db_session.add(cancelled)
    db_session.commit()

    # Materializer's stale view still has this order in its schedule output.
    order_service.apply_schedule(
        db_session,
        [
            ScheduledResult(
                order_id=cancelled.id,
                scheduled_date=date(2026, 5, 13),
                quantity=200,
            ),
        ],
    )

    db_session.refresh(cancelled)
    # Status preserved — load-bearing assertion of the cancel-race regression.
    assert cancelled.status == OrderStatus.cancelled


def test_apply_schedule_does_not_clear_processing_lock(db_session: Session) -> None:
    """``set_schedule_dates`` MUST NOT touch ``is_processing_locked``. The
    lock's lifecycle belongs to the producer↔worker(``compound_finalize``)
    pipeline exclusively. Pre-fix, materializer unconditionally cleared
    the lock as a side effect of writing schedule columns — which let a
    second user op slip in on a row whose compound was still queued (the
    producer set lock=True, materializer ran before the worker accepted
    the compound, lock cleared prematurely, second producer saw lock=False
    and built a stale compound on top).

    Verifies by seeding a row with lock=True, running ``apply_schedule``,
    and asserting the lock stays.
    """
    creator = _make_user(db_session, username="apply-sched-no-clear-lock")
    locked = Order(
        order_number="ORD-NO-CLEAR-LOCK",
        customer_name="ACME",
        wafer_quantity=300,
        requested_delivery_date=date(2026, 5, 14),
        created_by=creator.id,
        status=OrderStatus.pending,  # producer flipped to pending while lock held
        is_processing_locked=True,
    )
    db_session.add(locked)
    db_session.commit()

    order_service.apply_schedule(
        db_session,
        [
            ScheduledResult(
                order_id=locked.id,
                scheduled_date=date(2026, 5, 13),
                quantity=300,
            ),
        ],
    )

    db_session.refresh(locked)
    # Schedule columns get written.
    assert locked.scheduled_production_date == date(2026, 5, 13)
    # Lock untouched — load-bearing.
    assert locked.is_processing_locked is True
    # Status moved from pending→scheduled (pending is not in the terminal-status guard).
    assert locked.status == OrderStatus.scheduled


def test_list_for_scheduler_excludes_in_production_orders(db_session: Session) -> None:
    """Rebuild reconstructs the algorithm state from DB truth, but cannot
    represent the "partial production progress" of an in-production order —
    its remaining wafer_quantity isn't stored anywhere DB-recoverable.

    Replaying an in_production order at its full ``wafer_quantity`` through
    ``add_order`` during rebuild would (1) double-count today's already-
    produced wafers in capacity_tree, and (2) on the next advance_day,
    ``mark_completed_outside_set`` would flag the order as completed
    because the algorithm couldn't place it (deadline_too_far /
    capacity_exceeded → skipped from state). The order's physical
    production progress vanishes into a silent ``completed`` status.

    Fix is at the source: ``list_for_scheduler`` returns only
    ``status=scheduled`` orders so rebuild's add_order loop never touches
    in_production rows; their DB state is preserved as-is.
    """
    creator = _make_user(db_session, username="list-for-scheduler-skip-inprod")

    # An order currently in physical production today.
    in_prod = Order(
        order_number="ORD-INPROD",
        customer_name="ACME",
        wafer_quantity=500,
        requested_delivery_date=date(2026, 5, 14),
        created_by=creator.id,
        status=OrderStatus.in_production,
        scheduled_production_date=date(2026, 5, 12),
        expected_delivery_date=date(2026, 5, 12),
    )
    # A normal future-scheduled order.
    scheduled = Order(
        order_number="ORD-SCHED",
        customer_name="ACME",
        wafer_quantity=300,
        requested_delivery_date=date(2026, 5, 20),
        created_by=creator.id,
        status=OrderStatus.scheduled,
        scheduled_production_date=date(2026, 5, 15),
        expected_delivery_date=date(2026, 5, 15),
    )
    # Completed history — also must be excluded.
    completed = Order(
        order_number="ORD-DONE",
        customer_name="ACME",
        wafer_quantity=200,
        requested_delivery_date=date(2026, 5, 10),
        created_by=creator.id,
        status=OrderStatus.completed,
        scheduled_production_date=date(2026, 5, 10),
        expected_delivery_date=date(2026, 5, 10),
    )
    db_session.add_all([in_prod, scheduled, completed])
    db_session.commit()

    orders, creators = order_service.list_for_scheduler(db_session)

    returned_ids = {o.order_id for o in orders}
    # Only the future-scheduled order is fed back into rebuild.
    assert returned_ids == {scheduled.id}
    assert in_prod.id not in returned_ids
    assert completed.id not in returned_ids
    # Creators map mirrors the same filter.
    assert set(creators.keys()) == {scheduled.id}


# ---------------------------------------------------------------------------
# Case 8 smart-routing in update_order (Phase 2)
# ---------------------------------------------------------------------------


def _make_pinned_order(
    db: Session,
    *,
    creator_id: uuid.UUID,
    order_number: str,
    deadline: date,
    pin_day: date,
    quantity: int = 100,
) -> Order:
    """Build a row that's already gone through the scheduler:
    status=scheduled, is_pinned=True, pinned_production_date=pin_day.
    """
    order = Order(
        order_number=order_number,
        customer_name="ACME",
        wafer_quantity=quantity,
        requested_delivery_date=deadline,
        scheduled_production_date=pin_day,
        expected_delivery_date=pin_day,
        created_by=creator_id,
        status=OrderStatus.scheduled,
        is_pinned=True,
        pinned_production_date=pin_day,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_update_order_unpinned_pushes_remove_add_compound(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """PATCH on a non-pinned order pushes ``[remove(old), add(new)]``.

    Group = shrink for defer (deadline later) — matches old per-op rule.
    No pin / unpin appear in the compound for a never-pinned order.
    """
    creator = _make_user(db_session, username="sru-1")
    old_deadline = date.today() + timedelta(days=10)
    new_deadline = date.today() + timedelta(days=15)
    order = Order(
        order_number="ORD-NP",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=old_deadline,
        created_by=creator.id,
        status=OrderStatus.pending,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    req = UpdateOrderRequest(
        requested_delivery_date=new_deadline,  # defer
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    assert mock_enqueue.call_count == 1
    compound = mock_enqueue.call_args.args[0]
    assert compound.group == "shrink"  # defer = shrink
    op_kinds = [op.op for op in compound.ops]
    assert op_kinds == ["remove", "add"]
    assert compound.ops[0].deadline == old_deadline
    assert compound.ops[1].deadline == new_deadline


def test_update_order_qty_grow_with_deadline_later_is_grow_group(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """Regression for batch-admission monotonicity: a PATCH that BOTH grows
    qty AND defers the deadline must be classified ``grow``.

    Pre-fix rule was ``shrink if (qty_smaller OR deadline_later) else grow``,
    which mis-classified this case as shrink even though the cumulative
    per-day delta is net-additive (e.g. ``qty=100→10000, day3→day5`` lands
    +9900 on day5). Halving's prefix-feasibility monotonicity assumed
    shrink-group compounds were always net-non-additive, so a
    self-infeasible "shrink" head would slip past admission and the
    worker would binary-search-reject it. Strict-AND rule fixes that.
    """
    creator = _make_user(db_session, username="sru-grow-defer")
    old_deadline = date.today() + timedelta(days=10)
    new_deadline = date.today() + timedelta(days=15)
    order = Order(
        order_number="ORD-GROW-DEFER",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=old_deadline,
        created_by=creator.id,
        status=OrderStatus.pending,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    req = UpdateOrderRequest(
        wafer_quantity=2500,  # grew (clamped to CHECK constraint max)
        requested_delivery_date=new_deadline,  # defer
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    compound = mock_enqueue.call_args.args[0]
    assert compound.group == "grow"  # net-additive ⇒ grow


def test_update_order_qty_smaller_with_deadline_earlier_is_grow_group(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """Companion regression: qty smaller BUT deadline pulled earlier must
    also be ``grow``.

    The deadline-earlier move shifts demand to an earlier day; even if
    total qty drops, the cumulative prefix on the earlier day can spike
    above pre-PATCH levels. Strict-AND rule classifies any deadline
    tightening as grow, regardless of qty direction.
    """
    creator = _make_user(db_session, username="sru-smaller-earlier")
    # Relative dates so both old and new deadlines fall inside the
    # producer's [today+1, today+30] horizon. New deadline is pulled
    # earlier than old to trigger the grow-group classification.
    original_deadline = date.today() + timedelta(days=25)
    earlier_deadline = date.today() + timedelta(days=20)
    order = Order(
        order_number="ORD-SMALLER-EARLIER",
        customer_name="ACME",
        wafer_quantity=500,
        requested_delivery_date=original_deadline,
        created_by=creator.id,
        status=OrderStatus.pending,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    req = UpdateOrderRequest(
        wafer_quantity=100,  # smaller
        requested_delivery_date=earlier_deadline,  # earlier
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    compound = mock_enqueue.call_args.args[0]
    assert compound.group == "grow"


def test_update_order_pinned_with_compatible_change_auto_re_pins(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """Case 14: PATCH on a pinned order where (new deadline >= pin day) AND
    (new qty <= old qty) → compound is ``[unpin, remove, add, pin(same day)]``.

    "Compatible" = pin day's capacity won't be exceeded by the new values.
    Auto re-pin preserves the user's pin intent without them re-issuing it.
    """
    creator = _make_user(db_session, username="sru-2")
    pin_day = date.today() + timedelta(days=5)
    old_deadline = date.today() + timedelta(days=10)
    new_deadline = date.today() + timedelta(days=15)
    order = _make_pinned_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-PIN-OK",
        deadline=old_deadline,
        pin_day=pin_day,
        quantity=100,
    )

    req = UpdateOrderRequest(
        wafer_quantity=80,  # smaller — OK
        requested_delivery_date=new_deadline,  # deferred — still ≥ pin day
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    assert mock_enqueue.call_count == 1
    compound = mock_enqueue.call_args.args[0]
    op_kinds = [op.op for op in compound.ops]
    assert op_kinds == ["unpin", "remove", "add", "pin"]
    # The re-pin uses the SAME pin day, the new qty, and the new deadline.
    pin_op = compound.ops[3]
    assert pin_op.fake_deadline == pin_day
    assert pin_op.wafer_quantity == 80
    assert pin_op.deadline == new_deadline


def test_update_order_pinned_with_qty_increase_silent_drops_pin(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """Case 14 negative: qty increased, so pin day's capacity might overflow.
    Per spec we silent-drop the pin — compound is ``[unpin, remove, add]``
    WITHOUT the trailing pin. User can re-pin manually if desired.
    """
    creator = _make_user(db_session, username="sru-3")
    order = _make_pinned_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-PIN-DROP",
        deadline=date(2026, 5, 20),
        pin_day=date(2026, 5, 15),
        quantity=100,
    )

    req = UpdateOrderRequest(
        wafer_quantity=200,  # bigger — disqualifies auto-re-pin
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    compound = mock_enqueue.call_args.args[0]
    op_kinds = [op.op for op in compound.ops]
    assert op_kinds == ["unpin", "remove", "add"]


def test_update_order_pinned_with_deadline_before_pin_day_silent_drops_pin(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """Case 13: new deadline < pin day. Pin day is no longer a valid
    production day for this order, so silent-drop pin.
    """
    creator = _make_user(db_session, username="sru-4")
    # Relative dates so all three (old deadline, pin day, new deadline)
    # land in the producer's [today+1, today+30] horizon. New deadline
    # must be strictly before pin_day to trigger silent-drop.
    old_deadline = date.today() + timedelta(days=20)
    pin_day = date.today() + timedelta(days=15)
    new_deadline_before_pin = date.today() + timedelta(days=10)
    order = _make_pinned_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-PIN-DL",
        deadline=old_deadline,
        pin_day=pin_day,
        quantity=100,
    )

    req = UpdateOrderRequest(
        requested_delivery_date=new_deadline_before_pin,  # BEFORE pin day
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    compound = mock_enqueue.call_args.args[0]
    op_kinds = [op.op for op in compound.ops]
    assert op_kinds == ["unpin", "remove", "add"]


def test_update_order_notes_only_skips_compound(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """A PATCH that only touches non-scheduling fields (notes) doesn't push
    any compound to the queue. The order still flips back to ``pending``
    via the existing status guard, but the scheduler doesn't need to do
    anything.
    """
    creator = _make_user(db_session, username="sru-5")
    order = Order(
        order_number="ORD-NOTES",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 5, 20),
        created_by=creator.id,
        status=OrderStatus.pending,
        notes=None,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    req = UpdateOrderRequest(
        notes="rush job",
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    assert mock_enqueue.call_count == 0


def test_create_order_pushes_add_compound(db_session: Session, mock_enqueue: MagicMock) -> None:
    """create_order pushes a 1-op ``[add]`` compound (group=grow).

    Also confirms is_processing_locked is set so the frontend can disable
    edits while the worker handles the add.
    """
    creator = _make_user(db_session, username="sru-6")
    # Relative deadline keeps the request inside the producer's
    # [today+1, today+30] horizon regardless of when the suite runs.
    req = CreateOrderRequest(
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date.today() + timedelta(days=15),
    )
    order_service.create_order(db_session, req, creator)

    assert mock_enqueue.call_count == 1
    compound = mock_enqueue.call_args.args[0]
    assert compound.group == "grow"
    assert [op.op for op in compound.ops] == ["add"]


def test_delete_order_unpinned_pushes_remove_compound(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """delete_order on a non-pinned order pushes ``[remove]`` (group=shrink)."""
    creator = _make_user(db_session, username="sru-7")
    order = Order(
        order_number="ORD-DEL-NP",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 5, 20),
        created_by=creator.id,
        status=OrderStatus.pending,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    order_service.delete_order(db_session, order.id, creator)

    compound = mock_enqueue.call_args.args[0]
    assert compound.group == "shrink"
    assert [op.op for op in compound.ops] == ["remove"]


def test_delete_order_pinned_pushes_unpin_then_remove_compound(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """delete_order on a pinned order pushes ``[unpin, remove]``.

    Without the prepended unpin, the worker's membership guard would 反爆 —
    pinned orders live in ``pinned_orders``, not pq. Order service
    auto-handles this so producers don't have to.
    """
    creator = _make_user(db_session, username="sru-8")
    order = _make_pinned_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-DEL-P",
        deadline=date(2026, 5, 20),
        pin_day=date(2026, 5, 15),
    )

    order_service.delete_order(db_session, order.id, creator)

    compound = mock_enqueue.call_args.args[0]
    assert [op.op for op in compound.ops] == ["unpin", "remove"]


def test_cancel_order_scheduled_pushes_remove_compound_with_cancel_kind(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """cancel_order pushes the same shape as delete_order ([remove] / [unpin,
    remove], group=shrink) but with ``db_action.kind="cancel"`` so the
    worker on accept leaves ``is_deleted=False``.
    """
    creator = _make_user(db_session, username="sru-cancel-1")
    order = Order(
        order_number="ORD-CANCEL-NP",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 5, 20),
        created_by=creator.id,
        status=OrderStatus.scheduled,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    order_service.cancel_order(db_session, order.id, creator)

    compound = mock_enqueue.call_args.args[0]
    assert compound.group == "shrink"
    assert [op.op for op in compound.ops] == ["remove"]
    assert compound.db_action.kind == "cancel"


def test_cancel_order_pinned_pushes_unpin_then_remove_with_cancel_kind(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """cancel_order on a pinned + scheduled order pushes ``[unpin, remove]``
    with kind="cancel". Mirrors delete_order's pinned path.
    """
    creator = _make_user(db_session, username="sru-cancel-2")
    order = _make_pinned_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-CANCEL-P",
        deadline=date(2026, 5, 20),
        pin_day=date(2026, 5, 15),
    )
    # _make_pinned_order leaves status=pending; flip to scheduled so the
    # cancel guard passes (cancel only allows status=scheduled).
    order.status = OrderStatus.scheduled
    db_session.commit()
    db_session.refresh(order)

    order_service.cancel_order(db_session, order.id, creator)

    compound = mock_enqueue.call_args.args[0]
    assert [op.op for op in compound.ops] == ["unpin", "remove"]
    assert compound.db_action.kind == "cancel"


def test_cancel_order_rejects_non_scheduled_status(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """cancel_order only accepts ``status=scheduled``. ``pending`` /
    ``in_production`` / ``completed`` / ``cancelled`` all 409. The
    409 for pending is the load-bearing one — it forces the frontend to
    route those through ``DELETE /schedule/operations/{compound_id}``
    instead of stacking another compound on a row whose create compound
    is still in the queue.
    """
    from fastapi import HTTPException

    creator = _make_user(db_session, username="sru-cancel-guard")
    for blocked_status in (
        OrderStatus.pending,
        OrderStatus.in_production,
        OrderStatus.completed,
        OrderStatus.cancelled,
    ):
        order = Order(
            order_number=f"ORD-CANCEL-{blocked_status.value}",
            customer_name="ACME",
            wafer_quantity=100,
            requested_delivery_date=date(2026, 5, 20),
            created_by=creator.id,
            status=blocked_status,
        )
        db_session.add(order)
        db_session.commit()
        db_session.refresh(order)

        with pytest.raises(HTTPException) as exc_info:
            order_service.cancel_order(db_session, order.id, creator)
        assert exc_info.value.status_code == 409

    # None of the blocked attempts should have enqueued a compound.
    assert mock_enqueue.call_count == 0


def test_delete_after_cancel_uses_fast_path_no_compound(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """delete_order on an already-cancelled (but visible) row must take the
    DB-only fast path: set ``is_deleted=True``, write the
    ``order.deleted`` audit, and **NOT** enqueue a ``[remove]`` compound.

    Enqueueing a second ``[remove]`` would silently double-subtract from
    the segment trees (``pq_remove`` no-ops at the leaf level, but
    ``apply_batch_to_capacity`` / ``apply_batch_to_deadline`` still
    apply the delta) — phantom capacity / phantom released-deadline that
    corrupts later admission decisions. The fast path is the producer-
    side defense; ``_extract_batch_ops`` synthetic-pq skip is the
    worker-side bottom liner. This test pins the producer half.
    """
    creator = _make_user(db_session, username="sru-del-after-cancel")
    order = Order(
        order_number="ORD-DEL-AFTER-CANCEL",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 5, 20),
        created_by=creator.id,
        status=OrderStatus.cancelled,
        is_deleted=False,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    order_service.delete_order(db_session, order.id, creator)

    db_session.refresh(order)
    assert order.is_deleted is True, "fast path must soft-delete the row"
    assert order.status == OrderStatus.cancelled, "status stays cancelled"
    assert mock_enqueue.call_count == 0, (
        "fast path must skip the [remove] compound to avoid tree double-subtract"
    )

    # Audit row should exist for the soft-delete.
    actions = db_session.scalars(
        select(AuditLog.action).where(AuditLog.resource_id == order.id)
    ).all()
    assert "order.deleted" in actions


# ---------------------------------------------------------------------------
# PATCH-with-pin: pin / unpin folded into the regular update_order path
# ---------------------------------------------------------------------------


def test_update_order_pin_unpinned_order_to_specific_day(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """PATCH ``{pinned_production_date: <day>}`` on an unpinned order:

    - Compound shape: ``[remove, add, pin]`` (no unpin since not pinned before)
    - Group: ``grow`` (pinning shifts demand off natural EDF onto one day)
    - db_action: ``new_pinned_production_date_set=True``, value = the day

    Equivalent to the old raw ``POST /schedule/operations`` pin path, but
    now goes through the same row-locked PATCH endpoint that protects
    against the producer-side race.
    """
    creator = _make_user(db_session, username="sru-pin-new")
    # Relative dates: deadline must stay > pin_day and inside the horizon
    # regardless of the wall-clock day the suite runs on (a hardcoded
    # deadline went stale once "today" advanced past it).
    pin_day = date.today() + timedelta(days=5)
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-PIN-NEW",
        deadline=date.today() + timedelta(days=10),
    )

    req = UpdateOrderRequest(
        pinned_production_date=pin_day,
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    compound = mock_enqueue.call_args.args[0]
    # No qty/deadline change here, so remove+add use the unchanged values;
    # the existing order_number / deadline is fine for the pin op.
    op_kinds = [op.op for op in compound.ops]
    assert op_kinds == ["pin"], op_kinds  # pin-only since qty/deadline unchanged
    assert compound.ops[0].fake_deadline == pin_day
    assert compound.group == "grow"
    assert compound.db_action.new_pinned_production_date_set is True
    assert compound.db_action.new_pinned_production_date == pin_day
    assert compound.db_action.old_is_pinned is False


def test_update_order_change_pin_day_emits_unpin_and_pin(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """PATCH a pinned order's pin day to a different day:

    - Compound shape: ``[unpin, pin(new_day)]`` — no remove+add since
      qty/deadline didn't change.
    - Group depends on direction: pin moving later = shrink, pin moving
      earlier = grow.
    """
    creator = _make_user(db_session, username="sru-pin-move")
    # Relative dates so deadline > new_pin_day > old_pin_day stays inside
    # the [today + 1, today + HORIZON_DAYS] window regardless of when
    # the suite runs.
    old_pin_day = date.today() + timedelta(days=3)
    new_pin_day = date.today() + timedelta(days=8)
    order = _make_pinned_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-PIN-MOVE",
        deadline=date.today() + timedelta(days=15),
        pin_day=old_pin_day,
    )
    # Move pin LATER → cumulative demand only flatter or unchanged → shrink.

    req = UpdateOrderRequest(
        pinned_production_date=new_pin_day,
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    compound = mock_enqueue.call_args.args[0]
    assert [op.op for op in compound.ops] == ["unpin", "pin"]
    pin_op = compound.ops[1]
    assert pin_op.fake_deadline == new_pin_day
    assert compound.group == "shrink"  # later pin day = looser
    assert compound.db_action.new_pinned_production_date_set is True
    assert compound.db_action.new_pinned_production_date == new_pin_day
    assert compound.db_action.old_pinned_production_date == old_pin_day


def test_update_order_unpin_pinned_order(db_session: Session, mock_enqueue: MagicMock) -> None:
    """PATCH ``{pinned_production_date: null}`` on a pinned order:

    - Compound shape: ``[unpin]`` — qty/deadline unchanged, no need for
      remove+add; no pin op since target state is unpinned.
    - db_action: ``new_pinned_production_date_set=True``, value = None
    """
    creator = _make_user(db_session, username="sru-unpin")
    order = _make_pinned_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-UNPIN",
        deadline=date(2026, 6, 1),
        pin_day=date(2026, 5, 20),
    )

    req = UpdateOrderRequest(
        pinned_production_date=None,
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    compound = mock_enqueue.call_args.args[0]
    assert [op.op for op in compound.ops] == ["unpin"]
    assert compound.db_action.new_pinned_production_date_set is True
    assert compound.db_action.new_pinned_production_date is None
    assert compound.db_action.old_is_pinned is True


def test_update_order_pin_plus_qty_change_combined(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """PATCH that changes BOTH qty and pin day in one call:

    - Compound shape: ``[remove, add, pin(new_day)]`` — full transition
    - Sequence matters: remove old qty first so add can use new qty,
      then pin with the new qty/new_deadline + new fake_deadline.
    """
    creator = _make_user(db_session, username="sru-pin-qty")
    # Relative dates so deadline stays > pin_day inside the horizon
    # regardless of the suite's wall-clock day.
    pin_day = date.today() + timedelta(days=5)
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-PIN-QTY",
        deadline=date.today() + timedelta(days=10),
        quantity=100,
    )

    req = UpdateOrderRequest(
        wafer_quantity=200,  # grows
        pinned_production_date=pin_day,
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    compound = mock_enqueue.call_args.args[0]
    assert [op.op for op in compound.ops] == ["remove", "add", "pin"]
    add_op = compound.ops[1]
    pin_op = compound.ops[2]
    assert add_op.wafer_quantity == 200
    assert pin_op.wafer_quantity == 200
    assert pin_op.fake_deadline == pin_day
    # qty grew → grow group regardless of pin direction
    assert compound.group == "grow"


def test_update_order_unpin_plus_deadline_change_combined(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """PATCH that unpins AND extends deadline in one call:

    - Compound shape: ``[unpin, remove, add]`` — no pin op since target
      is unpinned, but unpin still needed for the original pinned order
      to leave pinned_orders before remove can fire.
    """
    creator = _make_user(db_session, username="sru-unpin-defer")
    order = _make_pinned_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-UNPIN-DEFER",
        deadline=date(2026, 5, 25),
        pin_day=date(2026, 5, 20),
    )

    req = UpdateOrderRequest(
        requested_delivery_date=date(2026, 6, 5),  # later
        pinned_production_date=None,  # unpin
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    compound = mock_enqueue.call_args.args[0]
    assert [op.op for op in compound.ops] == ["unpin", "remove", "add"]
    assert compound.db_action.new_pinned_production_date_set is True
    assert compound.db_action.new_pinned_production_date is None


def test_update_order_pin_day_after_deadline_rejects_422(
    db_session: Session, mock_enqueue: MagicMock
) -> None:
    """Sync-feedback guard: pinning to a day AFTER the order's deadline
    is nonsensical (the pin day is supposed to be the "produce by" day,
    must be ≤ the customer's hard deadline). API rejects with 422 instead
    of letting the worker discover via WS notification."""
    creator = _make_user(db_session, username="sru-pin-bad-day")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-PIN-BAD",
        deadline=date(2026, 6, 1),
    )

    req = UpdateOrderRequest(
        pinned_production_date=date(2026, 6, 15),  # AFTER deadline
        version_id=order.version_id,
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        order_service.update_order(db_session, order.id, req, creator)
    assert exc_info.value.status_code == 422
    assert "Pin date" in exc_info.value.detail
    # No compound was enqueued.
    assert mock_enqueue.call_count == 0


# ---------------------------------------------------------------------------
# Bug 2 — apply_schedule per-order SAVEPOINT isolates StaleDataError
# ---------------------------------------------------------------------------


def test_apply_schedule_skips_stale_row_and_continues_others(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent PATCH bumps Order A's ``version_id`` between
    ``rebuild_task``'s SELECT and ``apply_schedule``'s flush — the per-row
    SAVEPOINT around ``set_schedule_dates`` must roll back ONLY Order A's
    update, leaving Order B's update + the outer transaction intact.

    Pre-fix: the first ``StaleDataError`` crashed the session into
    ``PendingRollbackError`` and every subsequent order in the loop
    cascaded with the same error, turning ``rebuild_task`` into an
    unrecoverable abort.

    Post-fix: ``with db.begin_nested(): set_schedule_dates(...)``
    catches the stale row, logs ``order.schedule.apply_stale_skipped``,
    and continues. Order B's UPDATE goes through and the outer
    ``db.commit()`` succeeds.
    """
    from sqlalchemy.orm.exc import StaleDataError

    creator = _make_user(db_session, username="apply-sched-stale")
    order_a = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-STALE-A",
        deadline=date(2026, 5, 20),
    )
    order_b = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-STALE-B",
        deadline=date(2026, 5, 22),
    )

    # Patch set_schedule_dates to raise StaleDataError for order_a but
    # pass through to the real impl for order_b. Mimics the production
    # race where one row got bumped between SELECT and flush.
    real_set_dates = order_service.order_repo.set_schedule_dates

    def fake_set_dates(db: Session, *, order_id: uuid.UUID, **kw: object) -> object:
        if order_id == order_a.id:
            raise StaleDataError("simulated concurrent PATCH bumped version_id")
        return real_set_dates(db, order_id=order_id, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "app.services.order.order_repo.set_schedule_dates",
        fake_set_dates,
    )

    scheduled = [
        ScheduledResult(order_id=order_a.id, scheduled_date=date(2026, 5, 15), quantity=100),
        ScheduledResult(order_id=order_b.id, scheduled_date=date(2026, 5, 18), quantity=100),
    ]

    # Pre-fix this would crash with PendingRollbackError mid-loop;
    # post-fix it skips A and applies B.
    applied = order_service.apply_schedule(db_session, scheduled)

    # Only order_b was applied; order_a was skipped.
    assert applied == 1

    # Verify outer transaction committed cleanly — the session is usable
    # afterwards.
    db_session.refresh(order_b)
    assert order_b.scheduled_production_date == date(2026, 5, 18)
    assert order_b.status == OrderStatus.scheduled

    # order_a's dates were NOT written (the SAVEPOINT rolled back).
    db_session.refresh(order_a)
    assert order_a.scheduled_production_date is None


# ---------------------------------------------------------------------------
# Bug 1 (A) — SELECT FOR UPDATE serializes concurrent PATCH on same order
# ---------------------------------------------------------------------------


def test_update_order_uses_get_by_id_for_update_to_serialize_concurrent_patches(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    mock_enqueue: MagicMock,
) -> None:
    """``update_order`` must go through ``get_by_id_for_update`` (which
    issues ``SELECT ... FOR UPDATE``), not the lock-free ``get_by_id``.

    With the row lock, two concurrent PATCHes on the same order serialize
    at the SELECT — the second blocks until the first commits, then sees
    ``is_processing_locked=True`` and returns 409. Without it, both
    PATCHes could read the row at ``version_id=N``, both build a compound
    off the same old data, and both enqueue to Redis before either
    commits — letting a stale compound slip through to the worker and
    trigger ``SegmentTreeInvariantError``.

    We verify the lock is acquired by tracking which repo function got
    called. A unit-level test for the locking behavior is enough; an
    actual two-session race test would require threads + advisory waits
    and isn't worth the complexity for this guard.
    """
    creator = _make_user(db_session, username="lock-update-sru")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-LOCK-U",
        deadline=date(2026, 5, 20),
    )

    # Wrap get_by_id_for_update so we can verify it was the function that
    # actually executed (rather than the lock-free get_by_id).
    real_for_update = order_service.order_repo.get_by_id_for_update
    for_update_mock = MagicMock(side_effect=real_for_update)
    monkeypatch.setattr(
        "app.services.order.order_repo.get_by_id_for_update",
        for_update_mock,
    )

    # Spy on the lock-free variant — it must NOT be called from update_order.
    real_unlocked = order_service.order_repo.get_by_id
    unlocked_mock = MagicMock(side_effect=real_unlocked)
    monkeypatch.setattr(
        "app.services.order.order_repo.get_by_id",
        unlocked_mock,
    )

    req = UpdateOrderRequest(
        wafer_quantity=200,
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    # Row was fetched with FOR UPDATE; lock-free get_by_id was NOT used.
    assert for_update_mock.call_count == 1
    assert unlocked_mock.call_count == 0


def test_delete_order_uses_get_by_id_for_update_to_serialize_concurrent_deletes(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    mock_enqueue: MagicMock,
) -> None:
    """Same row-lock guarantee for ``delete_order`` — two concurrent
    DELETEs on the same order must serialize at the SELECT so only one
    builds + enqueues the soft-delete compound.
    """
    creator = _make_user(db_session, username="lock-delete-sru")
    order = _make_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-LOCK-D",
        deadline=date(2026, 5, 20),
    )

    real_for_update = order_service.order_repo.get_by_id_for_update
    for_update_mock = MagicMock(side_effect=real_for_update)
    monkeypatch.setattr(
        "app.services.order.order_repo.get_by_id_for_update",
        for_update_mock,
    )

    order_service.delete_order(db_session, order.id, creator)

    assert for_update_mock.call_count == 1
