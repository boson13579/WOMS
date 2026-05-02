"""Order business logic.

This layer owns all domain rules: status guards, order_number generation,
optimistic-lock error translation, audit logging, and soft-delete semantics.
It accepts and returns Pydantic schemas — never raw SQLAlchemy rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.logger import audit_log as emit_audit_log
from app.models.order import MUTABLE_STATUSES, Order, OrderStatus
from app.models.user import User
from app.repositories import audit_log as audit_log_repo
from app.repositories import order as order_repo
from app.schemas.order import (
    AuditLogResponse,
    BatchUpdateRequest,
    BatchUpdateResponse,
    CreateOrderRequest,
    OrderListResponse,
    OrderResponse,
    UpdateOrderRequest,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "batch_update_orders",
    "create_order",
    "delete_order",
    "get_audit_log",
    "get_order",
    "list_orders",
    "update_order",
]

_IMMUTABLE_STATUS_ERROR = HTTPException(
    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    detail="Order cannot be modified in its current status.",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_order_number(db: Session) -> str:
    """Produce a unique ORD-YYYYMMDD-XXXX number for today."""
    today = datetime.now(tz=UTC).date()
    count = order_repo.get_today_order_count(db, today)
    seq = count + 1
    return f"ORD-{today.strftime('%Y%m%d')}-{seq:04d}"


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
    """Create an order, write audit log, and return the response schema."""
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
    logger.info("order.created", order_number=order.order_number, actor_id=str(actor.id))
    return OrderResponse.model_validate(order)


def list_orders(
    db: Session,
    *,
    status: list[OrderStatus] | None = None,
    assigned_to: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> OrderListResponse:
    """Return a paginated list of active orders with optional filters."""
    items, total = order_repo.get_many(
        db, status=status, assigned_to=assigned_to, page=page, page_size=page_size
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


def update_order(
    db: Session, order_id: uuid.UUID, req: UpdateOrderRequest, actor: User
) -> OrderResponse:
    """Update a mutable order with optimistic-lock and status guard."""
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    if order.status not in MUTABLE_STATUSES:
        raise _IMMUTABLE_STATUS_ERROR

    # Application-level optimistic lock: reject stale client versions before
    # making any changes. SQLAlchemy's DB-level check fires on flush(), but this
    # early guard gives a clearer error and avoids unnecessary DB work.
    if req.version_id != order.version_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order was modified by another user. Refresh and try again.",
        )

    old_val: dict[str, Any] = {
        "wafer_quantity": order.wafer_quantity,
        "requested_delivery_date": str(order.requested_delivery_date),
        "notes": order.notes,
        "status": order.status.value,
    }

    if req.wafer_quantity is not None:
        order.wafer_quantity = req.wafer_quantity
    if req.requested_delivery_date is not None:
        order.requested_delivery_date = req.requested_delivery_date
    if req.notes is not None:
        order.notes = req.notes
    order.status = OrderStatus.pending

    new_val: dict[str, Any] = {
        "wafer_quantity": order.wafer_quantity,
        "requested_delivery_date": str(order.requested_delivery_date),
        "notes": order.notes,
        "status": order.status.value,
    }

    _write_audit(
        db,
        action="order.updated",
        actor=actor,
        order=order,
        old_value=old_val,
        new_value=new_val,
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


def delete_order(db: Session, order_id: uuid.UUID, actor: User) -> OrderResponse:
    """Soft-delete an order by setting is_deleted=True and status=cancelled."""
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    old_val: dict[str, Any] = {"status": order.status.value, "is_deleted": False}
    order.is_deleted = True
    order.status = OrderStatus.cancelled

    _write_audit(db, action="order.cancelled", actor=actor, order=order, old_value=old_val)
    db.commit()
    db.refresh(order)
    logger.info("order.cancelled", order_id=str(order_id), actor_id=str(actor.id))
    return OrderResponse.model_validate(order)


def batch_update_orders(db: Session, req: BatchUpdateRequest, actor: User) -> BatchUpdateResponse:
    """Bulk-update delivery dates; silently skip immutable-status orders."""
    updated: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []

    for order_id in req.order_ids:
        order = order_repo.get_by_id(db, order_id)
        if order is None or order.status not in MUTABLE_STATUSES:
            skipped.append(order_id)
            continue

        old_date = str(order.requested_delivery_date)
        order.requested_delivery_date = req.requested_delivery_date
        order.status = OrderStatus.pending
        _write_audit(
            db,
            action="order.updated",
            actor=actor,
            order=order,
            old_value={"requested_delivery_date": old_date},
            new_value={"requested_delivery_date": str(req.requested_delivery_date)},
        )
        updated.append(order_id)

    db.commit()
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
    """Return all audit-log entries for an order; raise 404 if not found."""
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    logs = audit_log_repo.get_by_resource_id(db, order_id)
    return [AuditLogResponse.model_validate(log) for log in logs]
