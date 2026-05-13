"""Business logic for hard lock and soft pin operations.

Hard Lock (is_locked):   scheduler manually locks an order's production date;
                         the scheduling engine skips locked orders.
Soft Pin  (soft_pin_date): scheduler sets a preferred production date;
                           the engine respects it but may move if capacity is tight.

Both operations are idempotent. Redis / scheduling lock checks live in the router
layer — this service has no Redis dependency.

NOTE: Order.is_locked, Order.locked_by, Order.locked_at, Order.soft_pin_date,
LockResponse, and SoftPinResponse are defined on feat/order-lock-mechanism and
do not yet exist on main. The # type: ignore[attr-defined] markers suppress
mypy errors until that branch merges.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.logger import audit_log as emit_audit_log
from app.models.user import User, UserRole
from app.repositories import audit_log as audit_log_repo
from app.repositories import order as order_repo
from app.schemas.order import LockResponse, SoftPinResponse  # type: ignore[attr-defined]

logger = structlog.get_logger(__name__)

_OWNERSHIP_ERROR = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="You can only modify orders you created.",
)


def _guard_ownership(order: Any, actor: User) -> None:
    """Raise 403 if actor is order_manager and doesn't own the order."""
    if actor.role == UserRole.order_manager and order.created_by != actor.id:
        raise _OWNERSHIP_ERROR


def _write_lock_audit(
    db: Session,
    *,
    action: str,
    actor: User,
    order_id: uuid.UUID,
    old_value: dict[str, Any],
    new_value: dict[str, Any],
) -> None:
    audit_log_repo.create(
        db,
        action=action,
        user_id=actor.id,
        resource_type="order",
        resource_id=order_id,
        old_value=old_value,
        new_value=new_value,
    )


def lock_order(
    db: Session,
    order_id: uuid.UUID,
    actor: User,
) -> LockResponse:
    """Hard-lock an order: set is_locked=True.

    Idempotent — returns 200 even if already locked.
    """
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    _guard_ownership(order, actor)

    if order.is_locked:  # type: ignore[attr-defined]
        return LockResponse.model_validate(order)

    order = order_repo.update_lock(  # type: ignore[attr-defined]
        db,
        order,
        is_locked=True,
        locked_by=actor.id,
        locked_at=datetime.now(UTC),
    )
    old_val: dict[str, Any] = {"is_locked": False}
    new_val: dict[str, Any] = {"is_locked": True, "locked_by": str(actor.id)}
    _write_lock_audit(
        db,
        action="order.locked",
        actor=actor,
        order_id=order.id,
        old_value=old_val,
        new_value=new_val,
    )
    db.commit()
    db.refresh(order)
    emit_audit_log(
        action="order.locked",
        actor_id=str(actor.id),
        resource_type="order",
        resource_id=str(order.id),
        changes={"old": old_val, "new": new_val},
    )
    logger.info("order.locked", order_id=str(order.id), actor_id=str(actor.id))
    return LockResponse.model_validate(order)


def unlock_order(
    db: Session,
    order_id: uuid.UUID,
    actor: User,
) -> LockResponse:
    """Remove the hard lock from an order: set is_locked=False.

    Idempotent — returns 200 even if already unlocked.
    """
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    _guard_ownership(order, actor)

    if not order.is_locked:  # type: ignore[attr-defined]
        return LockResponse.model_validate(order)

    old_val: dict[str, Any] = {"is_locked": True, "locked_by": str(order.locked_by)}  # type: ignore[attr-defined]
    order = order_repo.update_lock(  # type: ignore[attr-defined]
        db,
        order,
        is_locked=False,
        locked_by=None,
        locked_at=None,
    )
    new_val: dict[str, Any] = {"is_locked": False}
    _write_lock_audit(
        db,
        action="order.unlocked",
        actor=actor,
        order_id=order.id,
        old_value=old_val,
        new_value=new_val,
    )
    db.commit()
    db.refresh(order)
    emit_audit_log(
        action="order.unlocked",
        actor_id=str(actor.id),
        resource_type="order",
        resource_id=str(order.id),
        changes={"old": old_val, "new": new_val},
    )
    logger.info("order.unlocked", order_id=str(order.id), actor_id=str(actor.id))
    return LockResponse.model_validate(order)


def set_soft_pin(
    db: Session,
    order_id: uuid.UUID,
    preferred_date: date,
    actor: User,
) -> SoftPinResponse:
    """Set (or update) the soft-pin preferred production date."""
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    _guard_ownership(order, actor)

    old_date = order.soft_pin_date  # type: ignore[attr-defined]
    order = order_repo.update_soft_pin(db, order, soft_pin_date=preferred_date)  # type: ignore[attr-defined]
    old_val: dict[str, Any] = {"soft_pin_date": str(old_date) if old_date else None}
    new_val: dict[str, Any] = {"soft_pin_date": str(preferred_date)}
    _write_lock_audit(
        db,
        action="order.soft_pinned",
        actor=actor,
        order_id=order.id,
        old_value=old_val,
        new_value=new_val,
    )
    db.commit()
    db.refresh(order)
    emit_audit_log(
        action="order.soft_pinned",
        actor_id=str(actor.id),
        resource_type="order",
        resource_id=str(order.id),
        changes={"old": old_val, "new": new_val},
    )
    logger.info("order.soft_pinned", order_id=str(order.id), actor_id=str(actor.id))
    return SoftPinResponse.model_validate(order)


def clear_soft_pin(
    db: Session,
    order_id: uuid.UUID,
    actor: User,
) -> SoftPinResponse:
    """Clear the soft-pin preferred production date."""
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    _guard_ownership(order, actor)

    if order.soft_pin_date is None:  # type: ignore[attr-defined]
        return SoftPinResponse.model_validate(order)

    old_date = order.soft_pin_date  # type: ignore[attr-defined]
    order = order_repo.update_soft_pin(db, order, soft_pin_date=None)  # type: ignore[attr-defined]
    old_val: dict[str, Any] = {"soft_pin_date": str(old_date)}
    new_val: dict[str, Any] = {"soft_pin_date": None}
    _write_lock_audit(
        db,
        action="order.soft_pin_cleared",
        actor=actor,
        order_id=order.id,
        old_value=old_val,
        new_value=new_val,
    )
    db.commit()
    db.refresh(order)
    emit_audit_log(
        action="order.soft_pin_cleared",
        actor_id=str(actor.id),
        resource_type="order",
        resource_id=str(order.id),
        changes={"old": old_val, "new": new_val},
    )
    logger.info("order.soft_pin_cleared", order_id=str(order.id), actor_id=str(actor.id))
    return SoftPinResponse.model_validate(order)
