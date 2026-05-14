"""Targeted tests for ``app.services.order`` paths touched by the scheduler.

Per RULES.md §5 (TDD), every functional change ships with a regression test.
The PR-review fix that made ``apply_schedule`` write into the ``audit_logs``
table — instead of only emitting a stdout audit line — is exactly the kind
of change that needs a real-DB assertion: a unit test with mocked sessions
would happily pass even if the row were never persisted.

Scope is intentionally narrow: just enough to lock the audit-DB-write
contract that the review feedback called out. Broader ``services/order``
coverage lives elsewhere.
"""

from __future__ import annotations

import uuid
from datetime import date
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
) -> Order:
    order = Order(
        order_number=order_number,
        customer_name="ACME",
        wafer_quantity=quantity,
        requested_delivery_date=deadline,
        created_by=creator_id,
        status=OrderStatus.pending,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_apply_schedule_persists_audit_row_per_order(db_session: Session) -> None:
    """Each order whose schedule is applied must land a row in ``audit_logs``
    with ``action="order.scheduled"``, ``user_id=None`` (system actor), and a
    ``new_value`` JSON containing the persisted dates and final status.

    Pre-fix this path only emitted a stdout log; if the log shipper missed it
    the schedule history was unrecoverable from the DB.
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

    # Mixed multi-day assignment for order_a (collapses to earliest/latest);
    # single-day for order_b — covers both branches of the fold inside
    # apply_schedule.
    scheduled = [
        ScheduledResult(order_id=order_a.id, scheduled_date=date(2026, 5, 12), quantity=60),
        ScheduledResult(order_id=order_a.id, scheduled_date=date(2026, 5, 13), quantity=40),
        ScheduledResult(order_id=order_b.id, scheduled_date=date(2026, 5, 14), quantity=100),
    ]

    applied = order_service.apply_schedule(db_session, scheduled)
    assert applied == 2

    rows = list(
        db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "order.scheduled")
            .where(AuditLog.resource_id.in_([order_a.id, order_b.id]))
            .order_by(AuditLog.created_at.asc())
        ).all()
    )

    assert len(rows) == 2
    by_order = {row.resource_id: row for row in rows}
    a_row = by_order[order_a.id]
    b_row = by_order[order_b.id]

    # System-driven scheduling has no human actor.
    assert a_row.user_id is None
    assert b_row.user_id is None
    assert a_row.resource_type == "order"
    # New_value carries the earliest/latest fold and the new status, so the
    # audit history alone is enough to answer "when was X scheduled?".
    assert a_row.new_value == {
        "scheduled_production_date": "2026-05-12",
        "expected_delivery_date": "2026-05-13",
        "status": OrderStatus.scheduled.value,
    }
    assert b_row.new_value == {
        "scheduled_production_date": "2026-05-14",
        "expected_delivery_date": "2026-05-14",
        "status": OrderStatus.scheduled.value,
    }


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


def test_apply_schedule_preserves_in_production_status(db_session: Session) -> None:
    """A boundary order whose status is already ``in_production`` (advance_day
    promoted it because its production day arrived) MUST NOT be demoted back
    to ``scheduled`` when materialize runs another pass.

    Scenario reproducing the bug: boundary order made some wafers today,
    rest carries into tomorrow. advance_day sets status=in_production and
    advances base_date; the carried portion is still in pq, so the next
    accepted compound triggers materialize_schedule_task. ``apply_schedule``
    will see this order in its ``ScheduledResult`` list (because it still
    has work scheduled tomorrow) and call ``set_schedule_dates`` — which
    pre-fix would unconditionally write status=scheduled, breaking:

    1. The UI label flips from "producing now" to "queued".
    2. ``advance_day_task::mark_completed_outside_set`` only collects
       rows with ``status='in_production'``, so once demoted the order
       can never be flipped to ``completed`` and gets stuck in scheduled
       forever.
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
    # Schedule columns updated to reflect the carried portion.
    assert boundary.scheduled_production_date == date(2026, 5, 13)
    assert boundary.expected_delivery_date == date(2026, 5, 13)
    assert boundary.daily_breakdown == [
        {"date": "2026-05-13", "quantity": 500},
    ]
    # Status preserved — this is the load-bearing assertion of the regression.
    assert boundary.status == OrderStatus.in_production


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
    order = Order(
        order_number="ORD-NP",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 5, 20),
        created_by=creator.id,
        status=OrderStatus.pending,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    req = UpdateOrderRequest(
        requested_delivery_date=date(2026, 5, 25),  # defer
        version_id=order.version_id,
    )
    order_service.update_order(db_session, order.id, req, creator)

    assert mock_enqueue.call_count == 1
    compound = mock_enqueue.call_args.args[0]
    assert compound.group == "shrink"  # defer = shrink
    op_kinds = [op.op for op in compound.ops]
    assert op_kinds == ["remove", "add"]
    assert compound.ops[0].deadline == date(2026, 5, 20)  # old
    assert compound.ops[1].deadline == date(2026, 5, 25)  # new


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
    order = Order(
        order_number="ORD-GROW-DEFER",
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 5, 20),
        created_by=creator.id,
        status=OrderStatus.pending,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    req = UpdateOrderRequest(
        wafer_quantity=2500,  # grew (clamped to CHECK constraint max)
        requested_delivery_date=date(2026, 5, 25),  # defer
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
    order = Order(
        order_number="ORD-SMALLER-EARLIER",
        customer_name="ACME",
        wafer_quantity=500,
        requested_delivery_date=date(2026, 5, 25),
        created_by=creator.id,
        status=OrderStatus.pending,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    req = UpdateOrderRequest(
        wafer_quantity=100,  # smaller
        requested_delivery_date=date(2026, 5, 20),  # earlier
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
    pin_day = date(2026, 5, 15)
    order = _make_pinned_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-PIN-OK",
        deadline=date(2026, 5, 20),
        pin_day=pin_day,
        quantity=100,
    )

    req = UpdateOrderRequest(
        wafer_quantity=80,  # smaller — OK
        requested_delivery_date=date(2026, 5, 25),  # deferred — still ≥ pin day
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
    assert pin_op.deadline == date(2026, 5, 25)


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
    order = _make_pinned_order(
        db_session,
        creator_id=creator.id,
        order_number="ORD-PIN-DL",
        deadline=date(2026, 5, 20),
        pin_day=date(2026, 5, 15),
        quantity=100,
    )

    req = UpdateOrderRequest(
        requested_delivery_date=date(2026, 5, 10),  # BEFORE pin day
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
    req = CreateOrderRequest(
        customer_name="ACME",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 5, 20),
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
