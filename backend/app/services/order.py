"""Order business logic.

This layer owns all domain rules: status guards, order_number generation,
optimistic-lock error translation, audit logging, and soft-delete semantics.
It accepts and returns Pydantic schemas — never raw SQLAlchemy rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.audit import record_audit
from app.models.order import MUTABLE_STATUSES, Order, OrderStatus
from app.models.user import User, UserRole
from app.repositories import audit_log as audit_log_repo
from app.repositories import order as order_repo
from app.repositories import schedule_daily_capacity as daily_cap_repo
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
from app.services.scheduling import (
    DeadlineOutOfRangeError,
    ScheduledResult,
    SchedulingOrder,
    validate_deadline_in_horizon,
)

if TYPE_CHECKING:
    # Lazy import at runtime (inside ``get_capacity_usage``) keeps the
    # import-time graph of this module small. Annotations resolve via
    # ``from __future__ import annotations`` so the lazy import is fine.
    from app.schemas.schedule import DailyCapacityUsageEntry

logger = structlog.get_logger(__name__)

__all__ = [
    "apply_schedule",
    "batch_update_orders",
    "cancel_order",
    "create_order",
    "delete_order",
    "get_audit_log",
    "get_capacity_usage",
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


def _validate_deadline_or_422(deadline: date) -> None:
    """Producer-side mirror of ``add_order`` / ``pin_order``'s deadline check.

    Fails fast with HTTP 422 (with a user-readable message) when the
    deadline is today, in the past, or beyond the horizon. The worker
    would catch the same case via ``abs_to_rel`` returning ``None`` and
    soft-cancel the order asynchronously, but doing it here gives the
    frontend synchronous feedback instead of a "POST 200 → WS
    compound_failed" two-step UX wart.

    "Today" is sourced from ``datetime.now(UTC).date()`` to match the
    typical scheduler ``base_date``. There's a tiny race window at 00:00
    UTC where the producer's "today" and the scheduler's ``base_date``
    can disagree by one day; the worker's check (defense in depth)
    catches that case and the user sees the same async cancel they would
    have seen before this validator existed. Acceptable trade-off — that
    race fires at most once per day per user and exactly when an
    order's deadline straddles the midnight boundary.
    """
    today = datetime.now(tz=UTC).date()
    try:
        validate_deadline_in_horizon(deadline, today)
    except DeadlineOutOfRangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        ) from exc


def _generate_order_number(db: Session) -> str:
    """Produce a unique ORD-YYYYMMDD-XXXX number for today."""
    today = datetime.now(tz=UTC).date()
    seq = order_repo.allocate_order_seq(db, today)
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


def _build_cancel_compound(order: Order, actor_id: uuid.UUID) -> ScheduleCompoundRequest:
    """Compound for a user-initiated cancel: same shape as delete, kind="cancel".

    The scheduler-side effect is identical to delete (``unpin`` if pinned,
    then ``remove`` — group=shrink). The only divergence is the worker's
    DB write on accept: ``status=cancelled`` but ``is_deleted`` stays
    ``False`` so the row remains in ``GET /orders``. See
    ``compound_finalize._apply_db_action_accept`` for the kind="cancel"
    branch.
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
            kind="cancel",
            actor_id=actor_id,
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
    pin_day_set: bool = False,
    new_pin_day: date | None = None,
) -> ScheduleCompoundRequest | None:
    """Build the schedule compound for a PATCH that may touch qty / deadline / pin.

    Folds **pin / unpin** into the regular PATCH path so the frontend
    doesn't need a separate raw-compound endpoint just to drag an order to
    a specific production day. Pin is just another field on the order
    resource (``is_pinned`` + ``pinned_production_date`` columns); the
    PATCH semantics for it match the existing ``notes`` / ``assigned_to``
    "set" pattern:

    * ``pin_day_set=False`` (client omitted ``pinned_production_date``) —
      pin state is left as-is **except** for the existing case-14
      auto-re-pin / silent-drop logic when qty / deadline change makes
      the original pin incompatible.
    * ``pin_day_set=True``, ``new_pin_day=None`` — explicit unpin.
    * ``pin_day_set=True``, ``new_pin_day=X`` — pin to day X (or change
      pin day if already pinned).

    Compound op sequence is decided by joining three orthogonal axes:

    * ``needs_remove_add`` — qty or deadline changed; need ``[remove, add]``
    * ``needs_unpin`` — order was pinned before; pin ops can't co-exist
      with a stale ``[remove]`` on the same order_id in worker pq guard
    * ``needs_pin`` — final state has the order pinned to some day

    Yielding sequences like ``[unpin, remove, add, pin]`` (pin transition
    on a qty/deadline change), ``[remove, add, pin]`` (pin a previously-
    unpinned order while also tweaking qty), ``[unpin]`` (pure unpin, no
    qty change), etc. Returns ``None`` when nothing material changed and
    no compound needs to fire.

    Group selection: ``shrink`` only when **all** axes monotonically
    release capacity — qty doesn't increase, deadline doesn't move
    earlier, and pin day doesn't move earlier (a pin to an earlier day
    increases prefix demand). Otherwise ``grow``. The strict-AND keeps
    a shrink-group compound's per-day cumulative delta non-positive
    everywhere, which is the invariant the worker's batch-admission
    halving relies on.
    """
    old_qty = order.wafer_quantity
    old_deadline = order.requested_delivery_date
    is_pinned_before = order.is_pinned
    old_pin_day = order.pinned_production_date

    qty_changed = new_qty != old_qty
    deadline_changed = new_deadline != old_deadline
    needs_remove_add = qty_changed or deadline_changed

    # Decide the final pin state. Two paths:
    # (a) Client explicitly set ``pinned_production_date`` — honor verbatim
    # (b) Client didn't touch pin — fall back to case-14 auto-re-pin /
    #     silent-drop based on qty/deadline change compatibility.
    if pin_day_set:
        target_pin_day = new_pin_day  # may be None = "unpin"
    elif is_pinned_before and old_pin_day is not None and needs_remove_add:
        # Implicit transition: was pinned + something changed → try auto-re-pin.
        # Same conditions as the original case-14 gate.
        can_repin = new_deadline >= old_pin_day and new_qty <= old_qty
        target_pin_day = old_pin_day if can_repin else None
    else:
        # Pin state untouched.
        target_pin_day = old_pin_day if is_pinned_before else None

    final_is_pinned = target_pin_day is not None
    pin_state_changed = (final_is_pinned != is_pinned_before) or (target_pin_day != old_pin_day)

    # Nothing material for the scheduler? Return None so caller skips enqueue.
    if not (needs_remove_add or pin_state_changed):
        return None

    needs_unpin = is_pinned_before
    needs_pin_op = final_is_pinned

    # Group classification: shrink only when all axes are non-additive.
    qty_non_growing = new_qty <= old_qty
    deadline_non_earlier = new_deadline >= old_deadline
    # Pin moving to an earlier day shifts demand forward → grow.
    pin_non_earlier = True
    if final_is_pinned and is_pinned_before:
        pin_non_earlier = target_pin_day >= old_pin_day  # type: ignore[operator]
    elif final_is_pinned and not is_pinned_before:
        # Newly pinning to ANY day pulls demand off the natural EDF
        # distribution onto that one day → treat as grow.
        pin_non_earlier = False
    group: Literal["shrink", "grow"] = (
        "shrink" if (qty_non_growing and deadline_non_earlier and pin_non_earlier) else "grow"
    )

    # Op qty/deadline for the post-remove-add segment use new values when
    # remove+add fires; if only pin changed (no qty/deadline change), the
    # add/pin reference the unchanged values.
    op_qty = new_qty if needs_remove_add else old_qty
    op_deadline = new_deadline if needs_remove_add else old_deadline

    ops: list[ScheduleOpInCompound] = []
    if needs_unpin:
        ops.append(
            ScheduleOpInCompound(
                op="unpin",
                order_id=order.id,
                order_number=order.order_number,
                wafer_quantity=old_qty,
                deadline=old_deadline,
            )
        )
    if needs_remove_add:
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
    if needs_pin_op:
        ops.append(
            ScheduleOpInCompound(
                op="pin",
                order_id=order.id,
                order_number=order.order_number,
                wafer_quantity=op_qty,
                deadline=op_deadline,
                fake_deadline=target_pin_day,
            )
        )

    # Worker writes the pin columns iff state actually changed. Otherwise
    # skip the column write so the audit log doesn't show a no-op flip.
    write_pin_cols = pin_state_changed

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
            new_pinned_production_date_set=write_pin_cols,
            new_pinned_production_date=target_pin_day,
            old_wafer_quantity=old_qty,
            old_requested_delivery_date=old_deadline,
            old_notes=order.notes,
            old_assigned_to=order.assigned_to,
            old_pinned_production_date=old_pin_day,
            old_is_pinned=is_pinned_before,
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
    """Persist an audit row and emit an ECS stdout record (dual-write).

    Thin wrapper around :func:`app.core.audit.record_audit` that keeps the
    ``actor: User`` / ``order: Order`` signature so per-caller sites don't
    need to unpack ids by hand.
    """
    record_audit(
        db,
        action=action,
        actor_id=actor.id,
        resource_type="order",
        resource_id=order.id,
        old_value=old_value,
        new_value=new_value,
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
    _validate_deadline_or_422(req.requested_delivery_date)
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

    Uses ``get_by_id_for_update`` to take a row-level lock. Concurrent
    PATCHes on the same order serialize at the SELECT — the second one
    blocks until the first commits, then sees ``is_processing_locked=True``
    and rejects with 409 below. Without the lock, two PATCHes could both
    read the row at ``version_id=N``, each build a compound with the same
    "old" values, both enqueue to Redis before either commits — and the
    worker would later trip ``SegmentTreeInvariantError`` on the second
    stale compound.
    """
    order = order_repo.get_by_id_for_update(db, order_id)
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

    # Pin field uses the same "set" sentinel pattern — ``None`` is a legal
    # client input (= "unpin"), distinct from "missing" (= "keep current").
    pin_day_set = "pinned_production_date" in req.model_fields_set
    new_pin_day = req.pinned_production_date if pin_day_set else order.pinned_production_date
    # Surface "pinning to a day after the deadline" as a 422 here so users get
    # synchronous feedback instead of a delayed WS ``compound_failed``. Pinning
    # to None (unpin) is always fine.
    if pin_day_set and new_pin_day is not None and new_pin_day > new_deadline:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Pin date cannot be after the order's delivery deadline.",
        )

    # Producer-side range check (B + C of the deadline-validation design):
    # mirrors ``add_order`` / ``pin_order``'s ``abs_to_rel`` rejection so
    # the user gets a synchronous 422 instead of "PATCH 200 → WS
    # compound_failed → order auto-cancelled". Only fire when the user
    # actually touched the deadline (otherwise an unchanged deadline
    # that's now overtaken by ``base_date`` advancing would falsely
    # reject a notes-only PATCH).
    if req.requested_delivery_date is not None:
        _validate_deadline_or_422(new_deadline)
    if pin_day_set and new_pin_day is not None:
        _validate_deadline_or_422(new_pin_day)

    pin_changed_explicitly = pin_day_set and (
        (new_pin_day is None) != (not order.is_pinned)
        or (new_pin_day is not None and new_pin_day != order.pinned_production_date)
    )
    scheduling_changed = (
        new_qty != order.wafer_quantity
        or new_deadline != order.requested_delivery_date
        or pin_changed_explicitly
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
        pin_day_set=pin_day_set,
        new_pin_day=new_pin_day,
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

    Uses ``get_by_id_for_update`` to take a row-level lock. Concurrent
    DELETE / PATCH on the same order serialize at the SELECT — same
    rationale as ``update_order``: prevents two producers from each
    building a compound off the same old data and enqueueing duplicate /
    stale ops into Redis.
    """
    order = order_repo.get_by_id_for_update(db, order_id)
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

    # Fast path: the row is already cancelled (user previously hit "取消
    # 訂單", or worker auto-cancelled a rejected create). The order is
    # NOT in pq or pinned_orders anymore — the cancel compound already
    # released the capacity. Enqueueing another ``[remove]`` here would
    # silently double-subtract from the segment trees: ``pq_remove`` no-
    # ops at the leaf level, but ``apply_batch_to_capacity`` / ``apply_
    # batch_to_deadline`` already applied the per-day delta, producing
    # phantom capacity / phantom released-deadline that throws off every
    # subsequent admission check on that day.
    #
    # The desired DB effect of "delete after cancel" is just
    # ``is_deleted=True`` (hide from list); ``status=cancelled`` is
    # already true. Do that synchronously, audit it, return. No
    # compound, no lock dance.
    if order.status == OrderStatus.cancelled:
        # Snapshot BEFORE mutation so ``old_value`` reflects the
        # pre-change state — and so the audit insert lands in the SAME
        # transaction as the row UPDATE. Pre-fix this was mutate →
        # commit → refresh → record_audit → commit, which broke the
        # "audit row + row UPDATE share one transaction" invariant —
        # a process crash between the two commits would leave the row
        # soft-deleted with no audit trail.
        old_value = {
            "status": order.status.value,
            "is_deleted": False,
            "wafer_quantity": order.wafer_quantity,
            "requested_delivery_date": str(order.requested_delivery_date),
            "notes": order.notes,
            "assigned_to": (str(order.assigned_to) if order.assigned_to is not None else None),
        }
        order.is_deleted = True
        record_audit(
            db,
            action="order.deleted",
            actor_id=actor.id,
            resource_type="order",
            resource_id=order.id,
            old_value=old_value,
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

        # WebSocket cancellation notification — symmetric with the
        # ``_is_cancel`` branch in ``compound_finalize`` (worker accept of
        # a non-fast-path delete fires the same event). Without this,
        # the frontend never sees the row leave the list and stays
        # stale until next manual refetch. Best-effort: failures don't
        # roll back the soft-delete that just committed.
        try:
            notification_service.create_notification(
                db,
                user_id=order.created_by,
                order_id=order.id,
                type="order_cancelled",
                message=f"訂單 {order.order_number} 已被取消",
            )
        except Exception:
            logger.warning(
                "notification.create_failed",
                order_id=str(order.id),
                user_id=str(order.created_by),
                exc_info=True,
            )

        logger.info(
            "order.delete_fast_path_cancelled",
            order_id=str(order_id),
            actor_id=str(actor.id),
        )
        return OrderResponse.model_validate(order)

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


def cancel_order(db: Session, order_id: uuid.UUID, actor: User) -> OrderResponse:
    """User-initiated cancel of a ``scheduled`` order.

    Producer side: same row-lock + enqueue dance as ``delete_order``,
    but the compound carries ``db_action.kind="cancel"``. On worker
    accept the order ends up at ``status=cancelled`` with
    ``is_deleted=False`` — the row stays in ``GET /orders`` so the user
    sees their cancelled order in the list (audit + recovery trail),
    distinguishing it from ``delete_order`` which sets
    ``is_deleted=True`` and removes the row from the list view.

    Status guard is strict: **only ``scheduled``** is cancellable.
    ``pending`` orders should be retracted via
    ``DELETE /schedule/operations/{compound_id}`` (the queued compound
    hasn't been processed yet); ``in_production`` / ``completed`` /
    ``cancelled`` are immutable here (the floor has already started or
    finished work on the wafer batch). 409 for any other status.
    """
    order = order_repo.get_by_id_for_update(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    if actor.role == UserRole.order_manager and order.created_by != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify orders you created.",
        )

    if order.status != OrderStatus.scheduled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Order in status '{order.status.value}' cannot be cancelled. "
                "Only 'scheduled' orders are cancellable; use DELETE "
                "/schedule/operations/{compound_id} to retract a pending order."
            ),
        )

    if order.is_processing_locked:
        raise _LOCKED_ORDER_ERROR

    compound = _build_cancel_compound(order, actor.id)

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

    logger.info("order.user_cancel_requested", order_id=str(order_id), actor_id=str(actor.id))
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


def list_scheduled_orders(
    db: Session, *, completed_since: date | None = None
) -> list[ScheduleResultResponse]:
    """Return every order on the production timeline, sorted by start date.

    Always includes ``scheduled`` + ``in_production`` rows. When
    ``completed_since`` is given, additionally returns ``completed``
    rows whose ``scheduled_production_date`` is on/after that date —
    the API layer typically passes today - 30 days so the calendar
    view shows a rolling month of history without dragging the entire
    archive every fetch.

    Reads the per-day breakdown straight from the DB column
    ``orders.daily_breakdown`` (JSONB) that the materializer keeps in
    sync. No live Redis state is consulted on this read path — the
    column IS the source of truth for "what will this order's
    day-by-day production look like under the most recent accepted
    schedule". A NULL column degrades to an empty ``daily_breakdown``
    list in the response.
    """
    rows = order_repo.get_timeline(db, completed_since=completed_since)
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


def get_capacity_usage(
    db: Session,
    *,
    base_date: date,
    daily_capacity: int,
    horizon_days: int,
) -> tuple[date, int, list[DailyCapacityUsageEntry]]:
    """Return the per-day used / remaining capacity series for the upcoming horizon.

    Looks at the ``schedule_daily_capacity`` cache (written by
    :func:`apply_schedule`) and joins it against the requested
    ``[base_date, base_date + horizon_days)`` window to produce a
    fixed-length entry list. Dates in the window with no snapshot row
    come back as ``used=0, remaining=daily_capacity`` so the frontend
    can render a contiguous timeline without filling gaps client-side.

    Pre-base_date snapshot rows are ignored (they represent yesterday
    and earlier, which the next materialization will clear naturally
    via ``replace_all``). Post-horizon rows are also ignored — the
    snapshot may carry a few of those during a fresh materialization
    before the next ``advance_day_task`` rolls them off, and we don't
    want them leaking into the dashboard.

    Returns ``(base_date, daily_capacity, entries)`` so the caller can
    forward the values to the response schema without re-reading
    settings.
    """
    # Local import: schemas/schedule imports DTOs we use here, and that
    # module imports from models.order — keeping this lazy avoids
    # adding to the import-time graph of services/order (which is hot
    # path for nearly every endpoint).
    from app.schemas.schedule import DailyCapacityUsageEntry  # noqa: PLC0415

    snapshot_rows = daily_cap_repo.get_all_ordered(db)
    used_by_date: dict[date, int] = {row.date: row.used_quantity for row in snapshot_rows}

    entries: list[DailyCapacityUsageEntry] = []
    for offset in range(horizon_days):
        d = base_date + timedelta(days=offset)
        used = used_by_date.get(d, 0)
        # Clamp remaining at 0 — used > daily_capacity is logically
        # impossible (admission control rejects it) but defending here
        # means a corrupt snapshot row doesn't surface as a negative
        # number on the dashboard.
        remaining = max(0, daily_capacity - used)
        entries.append(DailyCapacityUsageEntry(date=d, used=used, remaining=remaining))
    return base_date, daily_capacity, entries


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
    *,
    preserve_capacity_for: date | None = None,
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

    ``preserve_capacity_for`` (typically the scheduler's current
    ``base_date`` = "today"): forwarded to
    ``daily_cap_repo.replace_all`` to keep that day's snapshot row
    intact across the truncate-and-insert. Callers that DO write the
    today row themselves (``advance_day_task`` via the ``today_portion``
    entries) leave this ``None`` so their write goes through. Callers
    whose ``scheduled`` list never contains the today row
    (``materialize_schedule_task`` / ``rebuild_schedule_task``: their
    ``compute_schedule`` output starts at ``base_date + 1``) pass
    ``state.base_date`` so today's in_production capacity usage —
    written earlier by ``advance_day_task`` — survives.

    Returns the number of orders that were marked as scheduled.
    """
    # Capture the pre-materialize ``scheduled_production_date`` for every
    # ``status='scheduled'`` order BEFORE ``clear_scheduled_dates`` wipes
    # them to NULL. This lets the per-order loop below tell "real schedule
    # change" apart from "re-materialize with the same dates" — the
    # notification spam that fired ``order_status_changed`` on every
    # materialize even when nothing actually moved.
    #
    # Orders not in this map (newly-promoted from pending, or with a NULL
    # date) get ``None`` from ``.get()`` — treated as "first-time schedule"
    # which always counts as a change worth notifying.
    prior_scheduled_dates: dict[uuid.UUID, date] = {
        row.id: row.scheduled_production_date
        for row in order_repo.get_scheduled(db)
        if row.scheduled_production_date is not None
    }
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
    # Per-day capacity-usage accumulator. Same single pass as per-order
    # daily_breakdown writes — for each ScheduledResult we already iterate
    # we also bump the matching date's running total. Written via
    # ``daily_cap_repo.replace_all`` after the loop so the snapshot is
    # always a single atomic projection of the just-applied schedule
    # rather than incremental upserts (see repo docstring for why).
    daily_totals: dict[date, int] = {}
    # Collect data for notifications before commit; attributes expire after
    # commit. Tuple shape: (recipient_user_id, order_id, order_number,
    # old_date, new_date) — old_date is None for first-time-scheduled orders.
    _notif_queue: list[tuple[uuid.UUID, uuid.UUID, str, date | None, date]] = []
    for order_id, results in per_order.items():
        results.sort(key=lambda x: x.scheduled_date)
        earliest = results[0].scheduled_date
        latest = results[-1].scheduled_date
        daily_breakdown_payload: list[dict[str, str | int]] = [
            {"date": sr.scheduled_date.isoformat(), "quantity": int(sr.quantity)} for sr in results
        ]
        # Accumulate per-day totals for the snapshot. We do this BEFORE
        # the SAVEPOINT below so the accumulator reflects what
        # ``compute_schedule()`` planned, not what actually committed —
        # if a row gets skipped due to ``StaleDataError`` (concurrent
        # PATCH), the next materialization re-runs with the post-PATCH
        # state anyway, so this snapshot is "best-known plan", which is
        # what the capacity-usage dashboard wants.
        for sr in results:
            daily_totals[sr.scheduled_date] = daily_totals.get(sr.scheduled_date, 0) + int(
                sr.quantity
            )
        is_pinned = order_id in pinned_map

        # SAVEPOINT-isolate each per-order write so a single ``StaleDataError``
        # (= ``version_id`` mismatch because a concurrent PATCH bumped the row
        # between our SELECT and our flush) only rolls back THAT order's
        # update, not the whole outer transaction. Without this nested guard
        # the first stale row crashes the session into
        # ``PendingRollbackError`` and every subsequent order in the loop
        # follows, turning rebuild / advance_day into an unrecoverable
        # task abort.
        #
        # Skipping is functionally safe: the concurrent PATCH enqueued a
        # compound that ``run_scheduling_task`` will process next, which
        # triggers a fresh ``materialize_schedule_task`` that rewrites the
        # skipped order's ``scheduled_production_date`` / ``daily_breakdown``
        # — so the only thing we "lose" by skipping is a redundant DB write.
        try:
            with db.begin_nested():
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
                # Only notify when the user-visible production date actually
                # moves. Re-materialize-with-same-dates (the common case after
                # every accepted compound, due to fast/slow split) must not
                # flood the user's notification center.
                #
                # ``None`` in ``prior`` means the order wasn't in
                # ``status='scheduled'`` before this materialize — either a
                # fresh promotion from ``pending`` or a return from
                # ``cancelled``. Both count as "first-time scheduled" and
                # always notify.
                old_scheduled_date = prior_scheduled_dates.get(order_id)
                if old_scheduled_date != earliest:
                    # Recipients: order.created_by always; order.assigned_to
                    # also when set and different from creator (de-dup so a
                    # creator who self-assigned doesn't get the same
                    # notification twice).
                    recipients: list[uuid.UUID] = [order.created_by]
                    if order.assigned_to is not None and order.assigned_to != order.created_by:
                        recipients.append(order.assigned_to)
                    for recipient_id in recipients:
                        _notif_queue.append(
                            (
                                recipient_id,
                                order.id,
                                order.order_number,
                                old_scheduled_date,
                                earliest,
                            )
                        )
        except StaleDataError:
            # Concurrent PATCH on this row landed between our SELECT and
            # our flush. The SAVEPOINT is already rolled back; outer
            # transaction still usable. Log + skip.
            logger.warning(
                "order.schedule.apply_stale_skipped",
                order_id=str(order_id),
            )
            continue

    # Write the per-day capacity-usage snapshot in the same transaction
    # as the per-order daily_breakdown rows. Atomic with respect to the
    # outer commit — readers either see the previous snapshot or the new
    # one, never a half-applied mix. Snapshot may include dates where
    # the corresponding order's row write got SAVEPOINT-skipped above;
    # that's a tolerable transient (the next materialize will re-write
    # the snapshot with fresh data) and the alternative — recomputing
    # daily_totals from only successfully-written rows — adds bookkeeping
    # complexity for no real user-visible win.
    daily_cap_repo.replace_all(db, entries=daily_totals, preserve_date=preserve_capacity_for)

    db.commit()
    logger.info(
        "order.schedule.applied",
        applied=applied,
        snapshot_days=len(daily_totals),
    )

    for notif_user_id, notif_order_id, order_number, old_date, new_date in _notif_queue:
        # Two message templates so the user-facing wording stays natural in
        # both "first-time scheduled" and "rescheduled" cases. Both include
        # the order number + the new date so the notification center entry
        # is self-contained without clicking through.
        if old_date is None:
            message = f"訂單 {order_number} 已排定生產日期 {new_date.isoformat()}"
        else:
            message = (
                f"訂單 {order_number} 生產日期已從 "
                f"{old_date.isoformat()} 變更為 {new_date.isoformat()}"
            )
        try:
            notification_service.create_notification(
                db,
                user_id=notif_user_id,
                order_id=notif_order_id,
                type="order_status_changed",
                message=message,
            )
        except Exception:
            logger.warning(
                "notification.create_failed",
                order_id=str(notif_order_id),
                user_id=str(notif_user_id),
                exc_info=True,
            )

    return applied
