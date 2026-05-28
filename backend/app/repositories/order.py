"""Pure CRUD operations for the Order entity.

No business logic here — validation, status guards, and audit logging live in
`services/order.py`. Every query filters `is_deleted=False` automatically.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models.order import Order, OrderStatus

__all__ = [
    "allocate_order_seq",
    "clear_scheduled_dates",
    "create",
    "get_by_id",
    "get_by_id_including_deleted",
    "get_many",
    "get_scheduled",
    "get_scheduled_for_rebuild",
    "get_timeline",
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


def get_by_id_for_update(db: Session, order_id: uuid.UUID) -> Order | None:
    """Like ``get_by_id`` but takes a PostgreSQL row-level exclusive lock.

    Two concurrent transactions both calling this on the same ``order_id``
    serialize: the second blocks until the first commits / rolls back.
    After the first commits, the second's SELECT proceeds and sees the
    updated row state (``version_id`` bumped, ``is_processing_locked``
    likely set to True).

    Used by ``update_order`` / ``delete_order`` to close the producer-side
    race where two concurrent PATCH requests both read the row at the
    same ``version_id``, each build a compound off the same old data, and
    both enqueue to Redis before either has committed. Without the row
    lock, PostgreSQL OCC catches the second commit (``StaleDataError``)
    but the stale compound is already in the Redis queue — worker then
    processes it with mismatched ``wafer_quantity`` and trips
    ``SegmentTreeInvariantError``. With the lock, the second transaction
    blocks before building its compound; when it unblocks it sees
    ``is_processing_locked=True`` and gets rejected with 409 cleanly.

    Returns ``None`` for absent / soft-deleted orders, matching ``get_by_id``.
    """
    stmt = select(Order).where(Order.id == order_id, Order.is_deleted.is_(False)).with_for_update()
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
    assigned_to: list[uuid.UUID] | None = None,
    created_by: uuid.UUID | None = None,
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
    if assigned_to:
        base = base.where(Order.assigned_to.in_(assigned_to))
    if created_by is not None:
        base = base.where(Order.created_by == created_by)
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


def allocate_order_seq(db: Session, today: date) -> int:
    """Atomically allocate the next sequence number for today's orders.

    Uses an upsert on order_daily_seq so that concurrent callers always
    receive distinct values — no TOCTOU race is possible.
    """
    result = db.execute(
        text(
            "INSERT INTO order_daily_seq(date, last_seq) VALUES (:today, 1) "
            "ON CONFLICT (date) DO UPDATE "
            "SET last_seq = order_daily_seq.last_seq + 1 "
            "RETURNING last_seq"
        ),
        {"today": today},
    )
    return int(result.scalar_one())


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


def get_timeline(db: Session, *, completed_since: date | None = None) -> list[Order]:
    """Timeline view: every active order that has a place on the calendar.

    Returns ``scheduled`` + ``in_production`` always (= what
    :func:`get_scheduled` returns). When ``completed_since`` is given,
    additionally returns ``completed`` orders whose
    ``scheduled_production_date`` falls on or after that date. Without
    it, no ``completed`` rows are returned (= identical to
    :func:`get_scheduled`).

    Why a separate function: :func:`get_scheduled` is also called by
    ``apply_schedule`` to snapshot prior dates for the
    "only-notify-when-date-changed" dedup; adding ``completed`` rows
    there would bloat that snapshot with no behavioural benefit and
    muddy the function's name. ``get_timeline`` is explicitly for the
    user-facing calendar view (``GET /schedule/result``) where
    completed orders show as "已完成" badges on their production day.

    ``completed_since`` is a hard floor on production date, not a
    rolling window — caller (typically the API layer) decides the
    window length (today - 30 days for the default calendar view).
    """
    status_filter: tuple[OrderStatus, ...]
    if completed_since is None:
        status_filter = (OrderStatus.scheduled, OrderStatus.in_production)
    else:
        status_filter = (
            OrderStatus.scheduled,
            OrderStatus.in_production,
            OrderStatus.completed,
        )
    stmt = (
        select(Order)
        .where(Order.is_deleted.is_(False))
        .where(Order.status.in_(status_filter))
        .order_by(Order.scheduled_production_date.asc())
    )
    if completed_since is not None:
        # ``completed_since`` only restricts completed rows. Use
        # ``OR (status != completed)`` so scheduled / in_production
        # rows pass regardless of their date (they may have None).
        stmt = stmt.where(
            or_(
                Order.status != OrderStatus.completed,
                Order.scheduled_production_date >= completed_since,
            )
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
    quantity split) and the pin columns.

    ``daily_breakdown`` is expected to be a chronologically-sorted list of
    ``{"date": "YYYY-MM-DD", "quantity": int}`` dicts. Pass ``None`` (or
    omit) only if the order has no schedule info, in which case the
    column is set to NULL — but in normal apply_schedule flow the
    materializer always passes a non-empty list since the order is by
    definition currently scheduled.

    **Status preservation for terminal / in_production statuses**: this
    function flips ``status`` to ``scheduled`` only when the current
    status is NOT in ``(in_production, cancelled, completed)``.

    - ``in_production``: once ``advance_day_task::mark_in_production``
      promotes a row to ``in_production``, the materializer can still
      freely re-write its scheduling columns (the boundary case where
      today's portion finished and the remainder is rolled into
      tomorrow) but MUST NOT demote it back to ``scheduled``. Demoting
      would (1) silently flip the frontend's "currently producing"
      flag to "queued" mid-shift and (2) cause
      ``mark_completed_outside_set`` (which only collects rows with
      ``status='in_production'``) to skip the order on completion,
      leaving it stuck in ``scheduled`` forever.
    - ``cancelled``: a row that's been cancelled (either by user-cancel
      or worker auto-reject of a create compound) must not be silently
      un-cancelled. Pre-fix the materializer rewrote ``status=scheduled``
      on cancelled rows whenever its in-memory state still had the row
      in pq (race window between cancel-compound enqueue and the next
      materializer run), and the materializer's UPDATE could land
      AFTER the worker's cancel-write but before another user op fired
      — wiping out the cancel.
    - ``completed``: once ``advance_day_task`` flips a finished
      production run to ``completed``, the row is done. A late
      materializer pass over a stale schedule snapshot that still
      contains the order could otherwise resurrect it back to
      ``scheduled`` — same shape as the ``cancelled`` race, same fix.

    **``is_processing_locked`` no longer touched here**: pre-fix this
    function unconditionally cleared the lock under the assumption that
    "landing in apply_schedule means the worker has finished". That
    assumption broke once materializer became an independent Celery
    task with its own single-flight slot — a materializer started
    before a compound was enqueued could race with the still-pending
    compound's worker-accept, clear the lock prematurely, and let a
    second user op slip in on the same row. The producer↔worker
    pipeline now exclusively owns the lock lifecycle (producer sets,
    ``compound_finalize.perform_compound_db_action`` clears).

    Returns the refreshed entity, or `None` if the order is missing or
    soft-deleted (caller decides how to react).
    """
    stmt = select(Order).where(Order.id == order_id, Order.is_deleted.is_(False))
    order = db.scalars(stmt).first()
    if order is None:
        return None
    if order.status == OrderStatus.in_production:
        # ``advance_day_task`` wrote the full multi-day
        # ``daily_breakdown`` for this row at the boundary when it
        # entered production (the day's full production + any carry
        # into following days). Subsequent materializer passes only
        # see the REMAINING work in the in-memory state, so calling
        # ``set_schedule_dates`` on an in_production row would
        # overwrite the multi-day breakdown to a single-day one,
        # making the calendar lose the "today's portion" entry for
        # boundary orders. Skip — the schedule columns for
        # in_production rows are frozen until completion (where they
        # become "past" columns and the row gets
        # ``mark_completed_outside_set``'ed).
        return order
    order.scheduled_production_date = scheduled_production_date
    order.expected_delivery_date = expected_delivery_date
    order.daily_breakdown = daily_breakdown
    if order.status not in (
        OrderStatus.cancelled,
        OrderStatus.completed,
    ):
        order.status = OrderStatus.scheduled
    order.is_pinned = is_pinned
    order.pinned_production_date = pinned_production_date if is_pinned else None
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
