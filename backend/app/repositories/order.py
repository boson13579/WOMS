"""Pure CRUD operations for the Order entity.

No business logic here — validation, status guards, and audit logging live in
`services/order.py`. Every query filters `is_deleted=False` automatically.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models.order import Order, OrderStatus

__all__ = [
    "clear_scheduled_dates",
    "create",
    "get_by_id",
    "get_by_id_including_deleted",
    "get_many",
    "get_scheduled",
    "get_scheduled_for_rebuild",
    "get_today_order_count",
    "mark_completed_outside_set",
    "mark_in_production",
    "set_schedule_dates",
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


# ---------------------------------------------------------------------------
# Scheduling-related queries
# ---------------------------------------------------------------------------


def get_scheduled(db: Session) -> list[Order]:
    """Return every active order with status ``scheduled`` or ``in_production``.

    Both statuses represent an order on the production timeline that the
    frontend wants to show on ``GET /schedule/result``. ``scheduled`` =
    queued for a future day, ``in_production`` = its day arrived (locked in
    at the most recent ``advance_day``) and physical production is in
    progress. ``completed`` rows are excluded — they're shown elsewhere
    (e.g., a separate "history" view).

    Sorted by ``scheduled_production_date`` ascending so callers see a
    natural timeline.
    """
    stmt = (
        select(Order)
        .where(Order.is_deleted.is_(False))
        .where(Order.status.in_((OrderStatus.scheduled, OrderStatus.in_production)))
        .order_by(Order.scheduled_production_date.asc())
    )
    return list(db.scalars(stmt).all())


def get_scheduled_for_rebuild(db: Session) -> list[Order]:
    """Return only ``status=scheduled`` orders for ``rebuild_state``.

    Sibling of :func:`get_scheduled` with a critically different filter:
    **``in_production`` orders are EXCLUDED**. Rebuild reconstructs the
    algorithm state (segment trees + pq) from DB truth by replaying each
    order through ``add_order`` at its full ``wafer_quantity``. For an
    in-production order, "full quantity" is wrong — part of it was already
    produced today; the remainder is what the algorithm should track, but
    that boundary state lives only in the about-to-be-rebuilt Redis state
    and can't be recovered from DB columns alone.

    Replaying an in-production order at full qty would (1) double-count
    its already-produced wafers in capacity_tree / deadline_tree, and (2)
    on the next ``advance_day``, ``mark_completed_outside_set`` would
    flip it to ``completed`` because the algorithm never put it back into
    the pq (either ``deadline_too_far`` or ``capacity_exceeded`` skipped
    it) — losing the order's physical production progress entirely.

    The contract is: in-production orders keep their existing DB state
    untouched through a rebuild; the next ``advance_day_task`` will mark
    them ``completed`` based on real-time production data. The algorithm
    only tracks the *future* (scheduled) — today's physical reality is
    DB-owned.
    """
    stmt = (
        select(Order)
        .where(Order.is_deleted.is_(False))
        .where(Order.status == OrderStatus.scheduled)
        .order_by(Order.scheduled_production_date.asc())
    )
    return list(db.scalars(stmt).all())


def clear_scheduled_dates(db: Session) -> int:
    """Bulk-clear scheduling-state columns on every active scheduled order.

    Wipes the dates summary (``scheduled_production_date`` /
    ``expected_delivery_date``) plus the JSONB ``daily_breakdown`` AND the
    two pin columns (``is_pinned`` / ``pinned_production_date``). One
    bulk UPDATE is cheaper than touching each row twice;
    ``set_schedule_dates`` rewrites whatever's actually scheduled right
    after, so wiping wide is safe.

    Returns the number of rows touched.
    """
    stmt = (
        update(Order)
        .where(Order.is_deleted.is_(False))
        .where(Order.status == OrderStatus.scheduled)
        .values(
            scheduled_production_date=None,
            expected_delivery_date=None,
            daily_breakdown=None,
            is_pinned=False,
            pinned_production_date=None,
        )
    )
    # ``Session.execute`` is typed as ``Result[Any]`` but for an UPDATE it
    # actually returns a ``CursorResult`` which carries ``rowcount``.
    result = db.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def set_schedule_dates(
    db: Session,
    *,
    order_id: uuid.UUID,
    scheduled_production_date: date,
    expected_delivery_date: date,
    daily_breakdown: list[dict[str, str | int]] | None = None,
    is_pinned: bool = False,
    pinned_production_date: date | None = None,
) -> Order | None:
    """Mark an order as scheduled with full materialized per-day info.

    Writes the summary dates, the JSONB ``daily_breakdown`` (per-day
    quantity split) and the pin columns. ``is_processing_locked`` is
    always cleared — landing in ``apply_schedule`` means the worker has
    finished its op for this order and the frontend may unlock the row
    for editing again.

    ``daily_breakdown`` is expected to be a chronologically-sorted list of
    ``{"date": "YYYY-MM-DD", "quantity": int}`` dicts. Pass ``None`` (or
    omit) only if the order has no schedule info, in which case the
    column is set to NULL — but in normal apply_schedule flow the
    materializer always passes a non-empty list since the order is by
    definition currently scheduled.

    **Status preservation for in_production**: this function flips
    ``status`` to ``scheduled`` only when the current status is NOT
    ``in_production``. Once ``advance_day_task::mark_in_production``
    promotes an order's status to ``in_production``, the materializer
    can still freely re-write its scheduling columns (the boundary case
    where today's portion finished and the remainder is rolled into
    tomorrow) but MUST NOT demote it back to ``scheduled``. Demoting
    would (1) silently flip the frontend's "currently producing" flag
    to "queued" mid-shift and (2) cause
    ``mark_completed_outside_set`` (which only collects rows with
    ``status='in_production'``) to skip the order on completion,
    leaving it stuck in ``scheduled`` forever.

    Returns the refreshed entity, or `None` if the order is missing or
    soft-deleted (caller decides how to react).
    """
    stmt = select(Order).where(Order.id == order_id, Order.is_deleted.is_(False))
    order = db.scalars(stmt).first()
    if order is None:
        return None
    order.scheduled_production_date = scheduled_production_date
    order.expected_delivery_date = expected_delivery_date
    order.daily_breakdown = daily_breakdown
    if order.status != OrderStatus.in_production:
        order.status = OrderStatus.scheduled
    order.is_pinned = is_pinned
    order.pinned_production_date = pinned_production_date if is_pinned else None
    order.is_processing_locked = False
    db.flush()
    db.refresh(order)
    return order


# ---------------------------------------------------------------------------
# Status transitions driven by advance_day (Phase 3)
# ---------------------------------------------------------------------------


def mark_in_production(db: Session, order_ids: set[uuid.UUID]) -> int:
    """Bulk-flip the given orders' status to ``in_production``.

    Called by ``advance_day_task`` for orders whose scheduled production
    day is "today" (the day just locked in by the current advance_day
    invocation). Overrides ``apply_schedule``'s ``scheduled`` status from
    earlier in the same transaction, which is intentional — apply_schedule
    runs first to set scheduled_production_date / expected_delivery_date,
    then this overrides the status column for the locked-in subset.

    Returns the number of rows touched (0 if ``order_ids`` is empty —
    SQLAlchemy turns an empty IN clause into an always-false predicate).
    """
    if not order_ids:
        return 0
    stmt = (
        update(Order)
        .where(Order.is_deleted.is_(False))
        .where(Order.id.in_(order_ids))
        .values(status=OrderStatus.in_production)
    )
    result = db.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def mark_completed_outside_set(db: Session, alive_ids: set[uuid.UUID]) -> int:
    """Mark ``in_production`` orders no longer in *alive_ids* as ``completed``.

    Called by ``advance_day_task`` at the top of its run. Semantics: an
    order that WAS in_production yesterday (= currently has status
    'in_production') AND is NOT in the new scheduler state's living set
    (pq + pinned_orders) must have finished its production — its final
    portion was made on the day that just ended.

    Why "outside set" rather than a date-based check: a boundary order's
    last day might span 2-3 calendar days; the cleanest signal that it's
    done is "no longer in the state's pq/pinned_orders". Date math gets
    fragile around boundary orders.

    Returns the number of rows flipped to ``completed``.
    """
    stmt = (
        update(Order)
        .where(Order.is_deleted.is_(False))
        .where(Order.status == OrderStatus.in_production)
    )
    if alive_ids:
        # Exclude orders that are still scheduled in the live state.
        stmt = stmt.where(Order.id.notin_(alive_ids))
    stmt = stmt.values(status=OrderStatus.completed)
    result = db.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
