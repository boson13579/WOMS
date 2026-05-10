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

import bcrypt
from app.models.audit_log import AuditLog
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from app.services import order as order_service
from app.services.scheduling import ScheduledResult
from sqlalchemy import select
from sqlalchemy.orm import Session


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

    rows = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "order.scheduled")
    ).all()
    assert list(rows) == []
