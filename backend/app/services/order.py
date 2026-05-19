"""Order business logic.

This layer owns all domain rules: status guards, order_number generation,
optimistic-lock error translation, audit logging, and soft-delete semantics.
It accepts and returns Pydantic schemas — never raw SQLAlchemy rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal

import structlog
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.logger import audit_log as emit_audit_log
from app.models.order import MUTABLE_STATUSES, Order, OrderStatus
from app.models.user import User, UserRole
from app.repositories import audit_log as audit_log_repo
from app.repositories import order as order_repo
from app.repositories import user as user_repo
from app.schemas.order import (
    AuditLogResponse,
    BatchUpdateRequest,
    BatchUpdateResponse,
    CreateOrderRequest,
    OrderListResponse,
    OrderResponse,
    UpdateOrderRequest,
)
from app.schemas.schedule import (
    CompoundDbAction,
    DailyAssignment,
    ScheduleCompoundRequest,
    ScheduleOpInCompound,
    ScheduleResultResponse,
)
from app.services import notification as notification_service
from app.services.schedule_queue import enqueue_compound
from app.services.scheduling import ScheduledResult, SchedulingOrder

logger = structlog.get_logger(__name__)

__all__ = [
    "apply_schedule",
    "batch_update_orders",
    "create_order",
    "delete_order",
    "get_audit_log",
    "get_order",
    "list_for_scheduler",
    "list_orders",
    "list_scheduled_orders",
    "update_order",
]

_IMMUTABLE_STATUS_ERROR = HTTPException(
    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    detail="Order cannot be modified in its current status.",
)

# N-3 (PR-14 round-2 review): producer-side concurrency guard. After the first
# write of ``is_processing_locked=True``, the row already has an in-flight
# compound waiting for the worker. A second PATCH / DELETE from the same user
# (e.g. double-click) or a concurrent client would otherwise enqueue another
# compound on top of the first — worker still processes them serially so DB
# stays consistent, but the audit log gets two diff rows in misleading order
# and the frontend's lock UI is silently bypassed (the row is "locked" but
# the second PATCH succeeded with 200). Better: 409 fast, let the user see
# why their action didn't take.
_LOCKED_ORDER_ERROR = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail=(
        "Order is being processed by the scheduler; please wait for the in-flight action to finish."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_assigned_to_user(db: Session, assigned_to: uuid.UUID | None) -> None:
    """Raise 422 if assigned_to is non-null but refers to a non-existent user."""
    if assigned_to is not None and user_repo.get_by_id(db, assigned_to) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Assigned user not found.",
        )


def _generate_order_number(db: Session) -> str:
    """Produce a unique ORD-YYYYMMDD-XXXX number for today."""
    today = datetime.now(tz=UTC).date()
    count = order_repo.get_today_order_count(db, today)
    seq = count + 1
    return f"ORD-{today.strftime('%Y%m%d')}-{seq:04d}"


def _build_create_compound(order: Order, actor_id: uuid.UUID) -> ScheduleCompoundRequest:
    """Compound for a newly-created order: just an ``add``.

    Group=grow because the order is consuming new capacity. If a producer
    has a pending ``remove`` for an earlier version of this order_id (only
    possible if there's a race with delete), shrink-first ordering ensures
    the remove runs first; the add then either succeeds or fails-with-
    rollback on a clean state.

    The attached ``db_action`` tells the worker to **soft-delete the row
    on failure** — the producer pre-created the row (locked, status=
    pending), so on add failure (deadline_too_far / capacity_exceeded)
    the worker must clean up the orphan. On success the worker leaves
    the row alone; ``apply_schedule`` will fill its scheduling columns.
    """
    ops = [
        ScheduleOpInCompound(
            op="add",
            order_id=order.id,
            order_number=order.order_number,
            wafer_quantity=order.wafer_quantity,
            deadline=order.requested_delivery_date,
        ),
    ]
    return ScheduleCompoundRequest(
        group="grow",
        op_count=len(ops),
        ops=ops,
        requested_by=actor_id,
        db_action=CompoundDbAction(
            kind="create",
            actor_id=actor_id,
            new_wafer_quantity=order.wafer_quantity,
            new_requested_delivery_date=order.requested_delivery_date,
            new_notes_set=True,
            new_notes=order.notes,
            new_assigned_to_set=True,
            new_assigned_to=order.assigned_to,
        ),
    )


def _build_delete_compound(order: Order, actor_id: uuid.UUID) -> ScheduleCompoundRequest:
    """Compound for a soft-deleted order: ``unpin`` (if pinned) then ``remove``.

    Group=shrink because the order frees capacity. If the order was never
    actually scheduled (still status=pending when delete fires), the
    worker-side membership guard catches the no-op gracefully and emits
    ``schedule.compound_failed`` — producer can ignore or surface.

    The attached ``db_action.kind='delete'`` defers the actual soft-delete
    (``is_deleted=True`` / ``status=cancelled``) to the worker. Producer
    only commits ``is_processing_locked=True``; on success worker
    soft-deletes + audits, on failure worker just clears the lock.
    """
    ops: list[ScheduleOpInCompound] = []
    if order.is_pinned:
        ops.append(
            ScheduleOpInCompound(
                op="unpin",
                order_id=order.id,
                order_number=order.order_number,
                wafer_quantity=order.wafer_quantity,
                deadline=order.requested_delivery_date,
            )
        )
    ops.append(
        ScheduleOpInCompound(
            op="remove",
            order_id=order.id,
            order_number=order.order_number,
            wafer_quantity=order.wafer_quantity,
            deadline=order.requested_delivery_date,
        )
    )
    return ScheduleCompoundRequest(
        group="shrink",
        op_count=len(ops),
        ops=ops,
        requested_by=actor_id,
        db_action=CompoundDbAction(
            kind="delete",
            actor_id=actor_id,
            # Old values captured so the worker can audit-log the prior
            # state alongside ``is_deleted: False -> True``.
            old_wafer_quantity=order.wafer_quantity,
            old_requested_delivery_date=order.requested_delivery_date,
            old_notes=order.notes,
            old_assigned_to=order.assigned_to,
        ),
    )


def _build_patch_compound(
    *,
    order: Order,
    new_qty: int,
    new_deadline: date,
    actor_id: uuid.UUID,
    notes_set: bool = False,
    new_notes: str | None = None,
    assigned_to_set: bool = False,
    new_assigned_to: uuid.UUID | None = None,
) -> ScheduleCompoundRequest | None:
    """Build the schedule compound for a PATCH that may touch qty / deadline.

    Implements the **case-8 smart-routing rules** from
    ``docs/scheduling.md``:

    * No qty/deadline change → returns ``None``, caller skips the enqueue.
    * Order not pinned → ``[remove(old), add(new)]``.
    * Order pinned:
      * Always prepend ``unpin`` (worker can't process ``remove`` on a
        pinned order — membership guard would reject it).
      * Auto-re-pin to the same day **only when both** conditions hold:
        ``new_deadline >= old_pin_day`` AND ``new_qty <= old_qty``. Either
        condition failing means the pin day's capacity might be exceeded
        if we forced the re-pin; we silent-drop the pin (per case 13/14).

    Group selection: ``shrink`` only when **both** axes monotonically
    release capacity — qty doesn't increase (``new_qty <= old_qty``) AND
    deadline doesn't move earlier (``new_deadline >= old_deadline``).
    Otherwise ``grow``. The strict-AND keeps the per-day cumulative
    delta of a shrink-group compound non-positive everywhere, which is
    the invariant the worker's batch-admission halving relies on: a
    contiguous prefix of feasible compounds stays feasible when we
    drop tail compounds. Pre-rewrite this used OR (``qty_smaller OR
    deadline_later``), which mis-classified e.g. ``qty=100→10000,
    deadline=day3→day5`` as shrink even though it adds +9900 to day5's
    cumulative demand; that broke halving's prefix-feasibility
    monotonicity assumption and let self-infeasible compounds slip
    through admission. Every op in a compound shares one group, so
    pin / unpin ops prepended / appended inherit this classification.
    """
    old_qty = order.wafer_quantity
    old_deadline = order.requested_delivery_date

    qty_changed = new_qty != old_qty
    deadline_changed = new_deadline != old_deadline
    if not (qty_changed or deadline_changed):
        # PATCH affected only notes / immaterial fields — no need to bother
        # the scheduler.
        return None

    qty_non_growing = new_qty <= old_qty
    deadline_non_earlier = new_deadline >= old_deadline
    group: Literal["shrink", "grow"] = (
        "shrink" if (qty_non_growing and deadline_non_earlier) else "grow"
    )

    is_pinned_before = order.is_pinned
    pin_day = order.pinned_production_date

    ops: list[ScheduleOpInCompound] = []

    if is_pinned_before:
        ops.append(
            ScheduleOpInCompound(
                op="unpin",
                order_id=order.id,
                order_number=order.order_number,
                wafer_quantity=old_qty,
                deadline=old_deadline,
            )
        )

    ops.append(
        ScheduleOpInCompound(
            op="remove",
            order_id=order.id,
            order_number=order.order_number,
            wafer_quantity=old_qty,
            deadline=old_deadline,
        )
    )
    ops.append(
        ScheduleOpInCompound(
            op="add",
            order_id=order.id,
            order_number=order.order_number,
            wafer_quantity=new_qty,
            deadline=new_deadline,
        )
    )

    # Case 14 auto-re-pin gate. ALL of these must hold:
    #   - order was pinned before the PATCH;
    #   - the PATCH didn't make the new deadline cross the pin day
    #     (otherwise pin can't satisfy "fake_deadline ≤ deadline");
    #   - qty didn't grow (otherwise the pin day's capacity might overflow).
    # Failing any → silent drop pin (case 13 semantics extended to all
    # incompatible PATCHes, not just the deadline-before-pin one).
    if is_pinned_before and pin_day is not None:
        can_repin = new_deadline >= pin_day and new_qty <= old_qty
        if can_repin:
            ops.append(
                ScheduleOpInCompound(
                    op="pin",
                    order_id=order.id,
                    order_number=order.order_number,
                    wafer_quantity=new_qty,
                    deadline=new_deadline,
                    fake_deadline=pin_day,
                )
            )

    return ScheduleCompoundRequest(
        group=group,
        op_count=len(ops),
        ops=ops,
        requested_by=actor_id,
        db_action=CompoundDbAction(
            kind="update",
            actor_id=actor_id,
            new_wafer_quantity=new_qty,
            new_requested_delivery_date=new_deadline,
            new_notes_set=notes_set,
            new_notes=new_notes,
            new_assigned_to_set=assigned_to_set,
            new_assigned_to=new_assigned_to,
            old_wafer_quantity=old_qty,
            old_requested_delivery_date=old_deadline,
            old_notes=order.notes,
            old_assigned_to=order.assigned_to,
        ),
    )


def _write_audit(
    db: Session,
    *,
    action: str,
    actor: User,
    order: Order,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    """Persist an audit row and emit an ECS stdout record."""
    audit_log_repo.create(
        db,
        action=action,
        user_id=actor.id,
        resource_type="order",
        resource_id=order.id,
        old_value=old_value,
        new_value=new_value,
    )
    emit_audit_log(
        action=action,
        actor_id=str(actor.id),
        resource_type="order",
        resource_id=str(order.id),
        changes={"old": old_value, "new": new_value},
    )


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


def create_order(db: Session, req: CreateOrderRequest, actor: User) -> OrderResponse:
    """Create an order, write audit log, and return the response schema.

    Newly-created orders enter the pending pool, so ``is_processing_locked``
    is set immediately — the frontend uses it to disable inline edits until
    the scheduler has applied the pending op (``apply_schedule`` clears it
    via ``set_schedule_dates``).
    """
    _validate_assigned_to_user(db, req.assigned_to)
    order_number = _generate_order_number(db)
    order = order_repo.create(
        db,
        order_number=order_number,
        customer_name=req.customer_name,
        wafer_quantity=req.wafer_quantity,
        requested_delivery_date=req.requested_delivery_date,
        created_by=actor.id,
        assigned_to=req.assigned_to,
        notes=req.notes,
    )
    order.is_processing_locked = True
    new_val: dict[str, Any] = {
        "customer_name": order.customer_name,
        "wafer_quantity": order.wafer_quantity,
        "requested_delivery_date": str(order.requested_delivery_date),
        "status": order.status.value,
        "assigned_to": str(order.assigned_to) if order.assigned_to is not None else None,
        "notes": order.notes,
    }
    _write_audit(db, action="order.created", actor=actor, order=order, new_value=new_val)
    db.commit()
    db.refresh(order)

    # Push the [add] compound to the scheduler queue. DB commit must
    # succeed first so the worker (which can read order state from DB on
    # rebuild) doesn't see ops for an order that hasn't landed yet.
    enqueue_compound(_build_create_compound(order, actor.id))

    logger.info("order.created", order_number=order.order_number, actor_id=str(actor.id))
    return OrderResponse.model_validate(order)


def list_orders(
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
) -> OrderListResponse:
    """Return a paginated list of active orders with optional filters."""
    items, total = order_repo.get_many(
        db,
        status=status,
        assigned_to=assigned_to,
        created_by=created_by,
        search=search,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return OrderListResponse(
        items=[OrderResponse.model_validate(o) for o in items],
        total=total,
        page=page,
        page_size=page_size,
    )


def get_order(db: Session, order_id: uuid.UUID) -> OrderResponse:
    """Fetch a single order by ID; raise 404 if not found."""
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return OrderResponse.model_validate(order)


def update_order(  # noqa: PLR0912, PLR0915
    db: Session, order_id: uuid.UUID, req: UpdateOrderRequest, actor: User
) -> OrderResponse:
    """Update a mutable order with optimistic-lock and status guard.

    **Producer-side responsibilities only** (per P1-2): validate, write
    ``is_processing_locked=True`` (and ``status=pending``) to claim the
    row, and enqueue a compound whose ``db_action`` carries the new
    values. The actual write of new ``wafer_quantity`` /
    ``requested_delivery_date`` / ``notes`` / ``assigned_to`` happens
    inside ``run_scheduling_task`` after the algorithm state has
    accepted the change. This eliminates the prior failure mode where
    a rejected compound (capacity_exceeded etc.) would leave DB with
    new values while ``SchedulerState`` rolled back to the old ones.

    For PATCHes that don't touch scheduling fields (notes / assigned_to
    only), we short-circuit — write everything directly in the producer
    since there's no compound to defer to.
    """
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    if actor.role == UserRole.order_manager and order.created_by != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify orders you created.",
        )

    if order.status not in MUTABLE_STATUSES:
        raise _IMMUTABLE_STATUS_ERROR

    # N-3 round-2 guard: if a previous compound is still in flight, refuse
    # to stack another one on top. See ``_LOCKED_ORDER_ERROR`` docstring.
    if order.is_processing_locked:
        raise _LOCKED_ORDER_ERROR

    # Application-level optimistic lock: reject stale client versions before
    # making any changes. SQLAlchemy's DB-level check fires on flush(), but this
    # early guard gives a clearer error and avoids unnecessary DB work.
    if req.version_id != order.version_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order was modified by another user. Refresh and try again.",
        )

    # Decide payload first — what fields did the user actually touch?
    new_qty = req.wafer_quantity if req.wafer_quantity is not None else order.wafer_quantity
    new_deadline = (
        req.requested_delivery_date
        if req.requested_delivery_date is not None
        else order.requested_delivery_date
    )
    notes_set = "notes" in req.model_fields_set
    new_notes = req.notes if notes_set else order.notes
    assigned_to_set = "assigned_to" in req.model_fields_set
    if assigned_to_set and actor.role not in (UserRole.scheduler, UserRole.root):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only scheduler and root can reassign orders.",
        )
    new_assigned_to = req.assigned_to if assigned_to_set else order.assigned_to
    if assigned_to_set:
        _validate_assigned_to_user(db, new_assigned_to)

    scheduling_changed = (
        new_qty != order.wafer_quantity or new_deadline != order.requested_delivery_date
    )

    if not scheduling_changed:
        # Non-scheduling PATCH (notes / assigned_to) — no worker round-trip needed.
        # Write directly and audit here; the producer remains the single
        # writer because no compound is ever enqueued.
        old_val: dict[str, Any] = {
            "notes": order.notes,
            "assigned_to": str(order.assigned_to) if order.assigned_to is not None else None,
        }
        if notes_set:
            order.notes = req.notes
        if assigned_to_set:
            order.assigned_to = req.assigned_to
        new_val_simple: dict[str, Any] = {
            "notes": order.notes,
            "assigned_to": str(order.assigned_to) if order.assigned_to is not None else None,
        }
        _write_audit(
            db,
            action="order.updated",
            actor=actor,
            order=order,
            old_value=old_val,
            new_value=new_val_simple,
        )
        try:
            db.commit()
        except StaleDataError as exc:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Order was modified by another user. Refresh and try again.",
            ) from exc
        db.refresh(order)
        return OrderResponse.model_validate(order)

    # Scheduling change — pre-build the compound on the *pre-PATCH* order
    # snapshot so its ``db_action`` carries the right new/old values.
    compound = _build_patch_compound(
        order=order,
        new_qty=new_qty,
        new_deadline=new_deadline,
        actor_id=actor.id,
        notes_set=notes_set,
        new_notes=new_notes,
        assigned_to_set=assigned_to_set,
        new_assigned_to=new_assigned_to,
    )
    # ``_build_patch_compound`` returns ``None`` only when no qty/deadline
    # change was detected; we already guarded above (``scheduling_changed``
    # short-circuit), so this branch is unreachable. Narrow the type for
    # mypy without using an ``assert`` (banned by ruff S101).
    if compound is None:  # pragma: no cover — defensive
        raise RuntimeError("compound builder returned None for a scheduling-changed PATCH")

    # Producer-side write: only the in-flight lock. DO NOT mutate the
    # business columns — worker writes those on accept.
    order.status = OrderStatus.pending
    order.is_processing_locked = True

    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order was modified by another user. Refresh and try again.",
        ) from exc
    db.refresh(order)

    enqueue_compound(compound)
    try:
        notification_service.create_notification(
            db,
            user_id=order.created_by,
            order_id=order.id,
            type="order_locked",
            message=f"訂單 {order.order_number} 已被鎖定處理中",
        )
    except Exception:
        logger.warning(
            "notification.create_failed",
            order_id=str(order.id),
            user_id=str(order.created_by),
            exc_info=True,
        )
    return OrderResponse.model_validate(order)


def delete_order(db: Session, order_id: uuid.UUID, actor: User) -> OrderResponse:
    """Soft-delete an order by deferring DB writes to the worker.

    **Producer-side responsibilities only** (per P1-2): claim the row
    with ``is_processing_locked=True`` and enqueue a delete compound
    whose ``db_action.kind="delete"`` instructs the worker to perform
    the soft-delete (``is_deleted=True`` / ``status=cancelled``) plus
    audit log on accept. On compound failure (the worker's membership
    guard rejects a never-scheduled order's ``remove``, etc.) the worker
    clears the lock without deleting; the order remains alive.
    """
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    if actor.role == UserRole.order_manager and order.created_by != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify orders you created.",
        )

    # N-3 round-2 guard: refuse to stack another compound on top of an
    # already-locked row. See ``_LOCKED_ORDER_ERROR``.
    if order.is_processing_locked:
        raise _LOCKED_ORDER_ERROR

    # Build the compound from the *current* row state (the values the
    # worker will use to soft-delete + audit on accept).
    compound = _build_delete_compound(order, actor.id)

    # Producer-side write: only the in-flight lock. The actual
    # ``is_deleted`` / ``status=cancelled`` write happens in the worker.
    order.is_processing_locked = True

    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order was modified by another user. Refresh and try again.",
        ) from exc
    db.refresh(order)

    enqueue_compound(compound)
    try:
        notification_service.create_notification(
            db,
            user_id=order.created_by,
            order_id=order.id,
            type="order_locked",
            message=f"訂單 {order.order_number} 已被鎖定處理中",
        )
    except Exception:
        logger.warning(
            "notification.create_failed",
            order_id=str(order.id),
            user_id=str(order.created_by),
            exc_info=True,
        )

    logger.info("order.cancel_requested", order_id=str(order_id), actor_id=str(actor.id))
    return OrderResponse.model_validate(order)


def batch_update_orders(db: Session, req: BatchUpdateRequest, actor: User) -> BatchUpdateResponse:
    """Bulk-update delivery dates; silently skip immutable-status orders.

    Per P1-2: producer commits only the ``is_processing_locked=True`` /
    ``status=pending`` flags on each successfully-claimed row. The new
    ``requested_delivery_date`` value rides in each compound's
    ``db_action`` and is written by the worker on accept (or rolled back
    via lock-clear on reject). Compounds are staged in memory and only
    enqueued after the outer commit succeeds — a commit failure rolls
    back DB changes and leaves no orphan compounds in Redis.
    """
    updated: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []
    pending_compounds: list[ScheduleCompoundRequest] = []

    for order_id in req.order_ids:
        order = order_repo.get_by_id(db, order_id)
        if order is None or order.status not in MUTABLE_STATUSES:
            skipped.append(order_id)
            continue
        # N-3: same in-flight guard as ``update_order``. Skip (not raise)
        # since this is a best-effort batch — the caller's
        # ``BatchUpdateResponse.skipped_ids`` already documents partial
        # failures.
        if order.is_processing_locked:
            skipped.append(order_id)
            continue

        if actor.role == UserRole.order_manager and order.created_by != actor.id:
            skipped.append(order_id)
            continue

        savepoint = db.begin_nested()
        try:
            # Build the compound BEFORE the lock-only write so the
            # ``db_action.old_*`` audit fields capture pre-PATCH values.
            compound = _build_patch_compound(
                order=order,
                new_qty=order.wafer_quantity,
                new_deadline=req.requested_delivery_date,
                actor_id=actor.id,
            )

            # Producer-side write: only the in-flight lock. Worker
            # writes ``requested_delivery_date`` on accept.
            order.status = OrderStatus.pending
            order.is_processing_locked = True
            db.flush()
            savepoint.commit()
            updated.append(order_id)

            if compound is not None:
                pending_compounds.append(compound)
        except StaleDataError:
            savepoint.rollback()
            db.expire_all()
            skipped.append(order_id)
            logger.warning(
                "order.batch_update_conflict",
                order_id=str(order_id),
                actor_id=str(actor.id),
            )

    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        logger.warning(
            "order.batch_update_commit_conflict",
            updated=len(updated),
            skipped=len(skipped),
            actor_id=str(actor.id),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="One or more orders were modified by another request. Please retry.",
        ) from exc

    # Outer commit succeeded — now flush the staged compounds.
    for compound in pending_compounds:
        enqueue_compound(compound)

    logger.info(
        "order.batch_updated",
        updated=len(updated),
        skipped=len(skipped),
        actor_id=str(actor.id),
    )
    return BatchUpdateResponse(
        updated_count=len(updated),
        skipped_count=len(skipped),
        skipped_ids=skipped,
    )


def get_audit_log(db: Session, order_id: uuid.UUID, current_user: User) -> list[AuditLogResponse]:
    """Return all audit-log entries for an order; raise 404 if not found.

    Uses get_by_id_including_deleted so cancelled orders remain queryable —
    their audit trail must always be accessible after soft-delete.
    """
    order = order_repo.get_by_id_including_deleted(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    logs = audit_log_repo.get_by_resource_id(db, order_id)
    return [AuditLogResponse.model_validate(log) for log in logs]


# ---------------------------------------------------------------------------
# Scheduling-related service operations
# ---------------------------------------------------------------------------


def list_scheduled_orders(db: Session) -> list[ScheduleResultResponse]:
    """Return every order currently in ``scheduled`` status, sorted by start date.

    Reads the per-day breakdown straight from the DB column
    ``orders.daily_breakdown`` (JSONB) that the materializer keeps in sync.
    No live Redis state is consulted on this read path — the column IS the
    source of truth for "what will this order's day-by-day production look
    like under the most recent accepted schedule". A NULL column degrades
    to an empty ``daily_breakdown`` list in the response.
    """
    rows = order_repo.get_scheduled(db)
    out: list[ScheduleResultResponse] = []
    for r in rows:
        breakdown_payload: list[DailyAssignment] = []
        if r.daily_breakdown:
            for entry in r.daily_breakdown:
                breakdown_payload.append(
                    DailyAssignment(
                        date=date.fromisoformat(str(entry["date"])),
                        quantity=int(entry["quantity"]),
                    )
                )
        # Build the response directly instead of model_validate(r) →
        # model_copy: pydantic's ``from_attributes`` would otherwise try to
        # validate the raw ``Order.daily_breakdown`` (JSONB or None) against
        # ``list[DailyAssignment]`` and fail on the NULL case. Constructing
        # the schema explicitly lets us drop in our parsed breakdown_payload.
        out.append(
            ScheduleResultResponse(
                id=r.id,
                order_number=r.order_number,
                customer_name=r.customer_name,
                wafer_quantity=r.wafer_quantity,
                requested_delivery_date=r.requested_delivery_date,
                scheduled_production_date=r.scheduled_production_date,
                expected_delivery_date=r.expected_delivery_date,
                status=r.status,
                daily_breakdown=breakdown_payload,
            )
        )
    return out


def list_for_scheduler(
    db: Session,
) -> tuple[list[SchedulingOrder], dict[uuid.UUID, uuid.UUID]]:
    """Return *future* (scheduled) orders as ``SchedulingOrder`` plus a creators map.

    The second element is a ``order_id -> created_by`` mapping used by the
    rebuild flow to push ``schedule.rebuild_skipped`` WebSocket messages
    back to the original requester.

    Used by ``rebuild_state`` to reconstruct the segment trees and priority
    queue from DB truth after a migration or state corruption. The deadline
    maps to ``requested_delivery_date``, consistent with how ops are enqueued
    via ``POST /schedule/operations``.

    **In-production orders are intentionally excluded** (via
    :func:`repositories.order.get_scheduled_for_rebuild`). They represent
    physical wafers being processed today whose remaining quantity isn't
    knowable from DB columns alone — replaying them at full
    ``wafer_quantity`` would corrupt the segment trees and risk silently
    flipping them to ``completed`` on the next ``advance_day_task``. The
    rebuild therefore reconstructs only the future timeline; today's
    in-flight production keeps its existing DB state until physical
    production wraps up and ``advance_day_task::mark_completed_outside_set``
    transitions it normally.
    """
    rows = order_repo.get_scheduled_for_rebuild(db)
    orders = [
        SchedulingOrder(
            order_id=r.id,
            order_number=r.order_number,
            wafer_quantity=r.wafer_quantity,
            deadline=r.requested_delivery_date,
            # Pin info is read at rebuild time so pinned orders land back
            # in pinned_orders rather than the pq. Only populate when the
            # row has both flags set — defends against a stale DB row with
            # is_pinned=true but no pinned_production_date.
            pinned_production_date=(
                r.pinned_production_date if r.is_pinned and r.pinned_production_date else None
            ),
        )
        for r in rows
    ]
    creators = {r.id: r.created_by for r in rows}
    return orders, creators


def apply_schedule(
    db: Session,
    scheduled: list[ScheduledResult],
    pinned: dict[uuid.UUID, date] | None = None,
) -> int:
    """Persist a freshly-computed schedule to the orders table.

    A single order can split across multiple days in `scheduled`; we collapse
    those rows to `(earliest, latest)` per order_id, wipe the previous
    schedule wholesale, then write the new dates and flip status to
    `scheduled`. One system-level audit record is emitted per order.

    ``pinned`` (optional) maps ``order_id -> fake_deadline`` and reflects the
    current ``state.pinned_orders``. Orders present in this map land in DB
    with ``is_pinned=true`` and ``pinned_production_date=fake_deadline``;
    orders absent from it have both pin columns cleared. Pass ``None`` (or
    omit) when no pin information is available — equivalent to "no orders
    are pinned".

    Returns the number of orders that were marked as scheduled.
    """
    order_repo.clear_scheduled_dates(db)
    pinned_map = pinned or {}

    # Group ScheduledResults by order_id and remember per-day quantities so
    # we can persist the full breakdown (not just earliest/latest summary)
    # to the JSONB column. Sort each per-order list by date so the stored
    # JSON is chronological — saves the read-path from re-sorting.
    per_order: dict[uuid.UUID, list[ScheduledResult]] = {}
    for sr in scheduled:
        per_order.setdefault(sr.order_id, []).append(sr)

    applied = 0
    # Collect data for notifications before commit; attributes expire after commit.
    _notif_queue: list[tuple[uuid.UUID, uuid.UUID, str, str]] = []
    for order_id, results in per_order.items():
        results.sort(key=lambda x: x.scheduled_date)
        earliest = results[0].scheduled_date
        latest = results[-1].scheduled_date
        daily_breakdown_payload: list[dict[str, str | int]] = [
            {"date": sr.scheduled_date.isoformat(), "quantity": int(sr.quantity)} for sr in results
        ]
        is_pinned = order_id in pinned_map
        order = order_repo.set_schedule_dates(
            db,
            order_id=order_id,
            scheduled_production_date=earliest,
            expected_delivery_date=latest,
            daily_breakdown=daily_breakdown_payload,
            is_pinned=is_pinned,
            pinned_production_date=pinned_map.get(order_id),
        )
        if order is None:
            logger.warning(
                "order.schedule.apply_missing",
                order_id=str(order_id),
            )
            continue
        applied += 1
        _notif_queue.append((order.created_by, order.id, order.order_number, order.status.value))
        # Read status from the refreshed row, not a hard-coded constant —
        # ``set_schedule_dates`` preserves ``in_production`` (see its
        # docstring for why), so an in-production order being re-materialized
        # for tomorrow's boundary portion will audit-log status=in_production,
        # not status=scheduled.
        new_value: dict[str, Any] = {
            "scheduled_production_date": str(earliest),
            "expected_delivery_date": str(latest),
            "status": order.status.value,
        }
        if is_pinned:
            new_value["pinned_production_date"] = str(pinned_map[order_id])
        # Persist to audit_logs DB table — required by PRD §1.6 so the
        # scheduling history is queryable from Postgres, not only from log
        # shippers. user_id=None marks this as system-driven.
        audit_log_repo.create(
            db,
            action="order.scheduled",
            user_id=None,
            resource_type="order",
            resource_id=order_id,
            new_value=new_value,
        )
        emit_audit_log(
            action="order.scheduled",
            actor_id=None,
            resource_type="order",
            resource_id=str(order_id),
            changes=new_value,
        )

    db.commit()
    logger.info("order.schedule.applied", applied=applied)

    for notif_user_id, notif_order_id, order_number, status_val in _notif_queue:
        try:
            notification_service.create_notification(
                db,
                user_id=notif_user_id,
                order_id=notif_order_id,
                type="order_status_changed",
                message=f"訂單 {order_number} 狀態已變更為 {status_val}",
            )
        except Exception:
            logger.warning(
                "notification.create_failed",
                order_id=str(notif_order_id),
                user_id=str(notif_user_id),
                exc_info=True,
            )

    return applied
