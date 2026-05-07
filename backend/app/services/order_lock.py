"""Business logic for hard pin and soft pin operations.

Hard Pin  (is_locked):   scheduler manually locks an order's production date;
                         the scheduling engine skips locked orders.
Soft Pin  (soft_pin_date): scheduler sets a preferred production date;
                           the engine respects it but may move if capacity is tight.

Both operations are idempotent. Redis / scheduling lock checks live in the router
layer — this service has no Redis dependency.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.logger import audit_log as emit_audit_log
from app.models.user import User
from app.repositories import audit_log as audit_log_repo
from app.repositories import order as order_repo
from app.schemas.order import LockResponse, SoftPinResponse

logger = structlog.get_logger(__name__)


def _write_lock_audit(
    db: Session,
    *,
    action: str,
    actor: User,
    order_id: uuid.UUID,
    old_value: dict[str, Any],
    new_value: dict[str, Any],
) -> None:
    """Persist an audit row to DB. Must be called within the open transaction (before commit)."""
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
    """Hard-pin an order: set is_locked=True.

    Idempotent — returns 200 even if already locked.
    """
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    if order.is_locked:
        return LockResponse.model_validate(order)

    order = order_repo.update_lock(
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
    """Remove the hard pin from an order: set is_locked=False.

    Idempotent — returns 200 even if already unlocked.
    """
    order = order_repo.get_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    if not order.is_locked:
        return LockResponse.model_validate(order)

    order = order_repo.update_lock(
        db,
        order,
        is_locked=False,
        locked_by=None,
        locked_at=None,
    )
    old_val2: dict[str, Any] = {"is_locked": True}
    new_val2: dict[str, Any] = {"is_locked": False}
    _write_lock_audit(
        db,
        action="order.unlocked",
        actor=actor,
        order_id=order.id,
        old_value=old_val2,
        new_value=new_val2,
    )
    db.commit()
    db.refresh(order)
    emit_audit_log(
        action="order.unlocked",
        actor_id=str(actor.id),
        resource_type="order",
        resource_id=str(order.id),
        changes={"old": old_val2, "new": new_val2},
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

    old_date = order.soft_pin_date
    order = order_repo.update_soft_pin(db, order, soft_pin_date=preferred_date)
    old_val3: dict[str, Any] = {"soft_pin_date": str(old_date) if old_date else None}
    new_val3: dict[str, Any] = {"soft_pin_date": str(preferred_date)}
    _write_lock_audit(
        db,
        action="order.soft_pinned",
        actor=actor,
        order_id=order.id,
        old_value=old_val3,
        new_value=new_val3,
    )
    db.commit()
    db.refresh(order)
    emit_audit_log(
        action="order.soft_pinned",
        actor_id=str(actor.id),
        resource_type="order",
        resource_id=str(order.id),
        changes={"old": old_val3, "new": new_val3},
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

    if order.soft_pin_date is None:
        return SoftPinResponse.model_validate(order)

    old_date = order.soft_pin_date
    order = order_repo.update_soft_pin(db, order, soft_pin_date=None)
    old_val4: dict[str, Any] = {"soft_pin_date": str(old_date)}
    new_val4: dict[str, Any] = {"soft_pin_date": None}
    _write_lock_audit(
        db,
        action="order.soft_pin_cleared",
        actor=actor,
        order_id=order.id,
        old_value=old_val4,
        new_value=new_val4,
    )
    db.commit()
    db.refresh(order)
    emit_audit_log(
        action="order.soft_pin_cleared",
        actor_id=str(actor.id),
        resource_type="order",
        resource_id=str(order.id),
        changes={"old": old_val4, "new": new_val4},
    )
    logger.info("order.soft_pin_cleared", order_id=str(order.id), actor_id=str(actor.id))
    return SoftPinResponse.model_validate(order)
