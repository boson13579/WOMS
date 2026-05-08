"""Pure CRUD operations for the Order entity.

No business logic here — validation, status guards, and audit logging live in
`services/order.py`. Every query filters `is_deleted=False` automatically.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models.order import Order, OrderStatus

__all__ = [
    "create",
    "get_by_id",
    "get_by_id_including_deleted",
    "get_many",
    "get_today_order_count",
]

SORTABLE_FIELDS: dict[str, InstrumentedAttribute[object]] = {
    "order_number": Order.order_number,
    "customer_name": Order.customer_name,
    "wafer_quantity": Order.wafer_quantity,
    "requested_delivery_date": Order.requested_delivery_date,
}
DEFAULT_SORT_BY = "requested_delivery_date"
DEFAULT_SORT_ORDER = "asc"


def get_by_id(db: Session, order_id: uuid.UUID) -> Order | None:
    """Return the order with *order_id*, or None if absent/soft-deleted."""
    stmt = select(Order).where(Order.id == order_id, Order.is_deleted.is_(False))
    return db.scalars(stmt).first()


def get_by_id_including_deleted(db: Session, order_id: uuid.UUID) -> Order | None:
    """Return the order with *order_id* regardless of soft-delete status.

    Used by audit-log queries so that cancelled orders remain queryable.
    """
    stmt = select(Order).where(Order.id == order_id)
    return db.scalars(stmt).first()


def get_many(
    db: Session,
    *,
    status: list[OrderStatus] | None = None,
    assigned_to: uuid.UUID | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[Order], int]:
    """Return a paginated list of active orders plus the total count."""
    base = select(Order).where(Order.is_deleted.is_(False))

    if status:
        base = base.where(Order.status.in_(status))
    if assigned_to is not None:
        base = base.where(Order.assigned_to == assigned_to)
    if search:
        trimmed = search.strip()
        if trimmed:
            escaped = trimmed.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            base = base.where(
                or_(
                    Order.order_number.ilike(pattern, escape="\\"),
                    Order.customer_name.ilike(pattern, escape="\\"),
                )
            )

    count_stmt = select(func.count()).select_from(base.subquery())
    total: int = db.scalars(count_stmt).one()

    field = SORTABLE_FIELDS.get(sort_by or DEFAULT_SORT_BY, SORTABLE_FIELDS[DEFAULT_SORT_BY])
    order_clause = field.asc() if (sort_order or DEFAULT_SORT_ORDER) == "asc" else field.desc()
    rows = db.scalars(
        base.order_by(order_clause, Order.id.asc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    return list(rows), total


def get_today_order_count(db: Session, today: date) -> int:
    """Return the number of orders whose order_number starts with today's prefix.

    Used to derive the daily sequence number for new order_numbers.
    """
    prefix = f"ORD-{today.strftime('%Y%m%d')}-"
    stmt = select(func.count()).where(
        Order.order_number.like(f"{prefix}%"),
    )
    return db.scalars(stmt).one()


def create(
    db: Session,
    *,
    order_number: str,
    customer_name: str,
    wafer_quantity: int,
    requested_delivery_date: date,
    created_by: uuid.UUID,
    assigned_to: uuid.UUID | None = None,
    notes: str | None = None,
) -> Order:
    """Insert a new Order row and return the refreshed entity."""
    order = Order(
        order_number=order_number,
        customer_name=customer_name,
        wafer_quantity=wafer_quantity,
        requested_delivery_date=requested_delivery_date,
        created_by=created_by,
        assigned_to=assigned_to,
        notes=notes,
    )
    db.add(order)
    db.flush()
    db.refresh(order)
    return order
