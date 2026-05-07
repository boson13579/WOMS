"""Order CRUD HTTP router.

Route registration order matters: /batch-update must precede /{order_id}
so FastAPI does not interpret the literal string "batch-update" as a UUID.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.redis import get_redis
from app.core.scheduling_lock import is_scheduling_locked
from app.core.security import require_roles
from app.models.order import OrderStatus
from app.models.user import User, UserRole
from app.schemas.order import (
    AuditLogResponse,
    BatchUpdateRequest,
    BatchUpdateResponse,
    CreateOrderRequest,
    LockResponse,
    OrderListResponse,
    OrderResponse,
    SoftPinRequest,
    SoftPinResponse,
    UpdateOrderRequest,
)
from app.services import order as order_service
from app.services import order_lock as order_lock_service

router = APIRouter()

# Roles allowed to read orders (order_manager and above)
_READ_ROLES = require_roles(UserRole.order_manager, UserRole.scheduler, UserRole.root)
# Roles allowed to write orders (scheduler and above)
_WRITE_ROLES = require_roles(UserRole.scheduler, UserRole.root)


def _scheduling_locked() -> HTTPException:
    return HTTPException(
        status_code=423,
        detail="Scheduling is in progress. Please try again later.",
    )


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
def create_order(
    request: CreateOrderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_WRITE_ROLES),
) -> OrderResponse:
    """Create a new wafer order.

    Permission: scheduler+.

    Errors:
        401: missing or invalid bearer token.
        403: role insufficient (viewer / order_manager).
        422: validation error (e.g. wafer_quantity out of range 25-2500).
    """
    return order_service.create_order(db, request, current_user)


@router.get("", response_model=OrderListResponse)
def list_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(_READ_ROLES),
    status_filter: Annotated[list[OrderStatus] | None, Query(alias="status")] = None,
    assigned_to: uuid.UUID | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> OrderListResponse:
    """List active orders with optional filtering and pagination.

    Permission: order_manager+.
    """
    return order_service.list_orders(
        db,
        status=status_filter,
        assigned_to=assigned_to,
        page=page,
        page_size=page_size,
    )


# IMPORTANT: /batch-update must be registered BEFORE /{order_id} to prevent
# FastAPI from matching the literal string "batch-update" as a UUID path param.
@router.patch("/batch-update", response_model=BatchUpdateResponse)
def batch_update_orders(
    request: BatchUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_WRITE_ROLES),
) -> BatchUpdateResponse:
    """Bulk-update the requested_delivery_date for multiple orders.

    Orders whose status is not pending or scheduled are silently skipped
    (not an error). Returns counts and IDs of updated vs skipped orders.

    Permission: scheduler+.
    """
    return order_service.batch_update_orders(db, request, current_user)


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(_READ_ROLES),
) -> OrderResponse:
    """Fetch a single order by ID.

    Permission: order_manager+.

    Errors:
        404: order not found or soft-deleted.
    """
    return order_service.get_order(db, order_id)


@router.patch("/{order_id}", response_model=OrderResponse)
async def update_order(
    order_id: uuid.UUID,
    request: UpdateOrderRequest,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(_WRITE_ROLES),
) -> OrderResponse:
    """Partially update an order (pending or scheduled only).

    Requires `version_id` for optimistic-lock validation.
    Status is automatically reset to `pending` after any update.

    Permission: scheduler+.

    Errors:
        404: order not found.
        409: version_id mismatch — another user modified the order.
        422: order is in an immutable status (in_production / completed / cancelled).
        423: scheduling engine is running — retry later.
    """
    if await is_scheduling_locked(redis):
        raise _scheduling_locked()
    return order_service.update_order(db, order_id, request, current_user)


@router.delete("/{order_id}", response_model=OrderResponse)
async def delete_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(_WRITE_ROLES),
) -> OrderResponse:
    """Soft-delete an order: sets is_deleted=True and status=cancelled.

    Returns the updated order record.

    Permission: scheduler+.

    Errors:
        404: order not found.
        423: scheduling engine is running — retry later.
    """
    if await is_scheduling_locked(redis):
        raise _scheduling_locked()
    return order_service.delete_order(db, order_id, current_user)


@router.get("/{order_id}/audit-log", response_model=list[AuditLogResponse])
def get_audit_log(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(_READ_ROLES),
) -> list[AuditLogResponse]:
    """Return all audit-log entries for a given order, oldest first.

    Permission: order_manager+.

    Errors:
        404: order not found.
    """
    return order_service.get_audit_log(db, order_id, current_user)


@router.post("/{order_id}/lock", response_model=LockResponse)
async def lock_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(_WRITE_ROLES),
) -> LockResponse:
    """Hard-pin an order: prevent the scheduling engine from moving it.

    Idempotent — calling again on an already-locked order returns 200.

    Permission: scheduler+.

    Errors:
        404: order not found.
        423: scheduling engine is running — retry later.
    """
    if await is_scheduling_locked(redis):
        raise _scheduling_locked()
    return order_lock_service.lock_order(db, order_id, current_user)


@router.delete("/{order_id}/lock", response_model=LockResponse)
async def unlock_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(_WRITE_ROLES),
) -> LockResponse:
    """Remove the hard pin from an order.

    Idempotent — calling again on an already-unlocked order returns 200.

    Permission: scheduler+.

    Errors:
        404: order not found.
    """
    if await is_scheduling_locked(redis):
        raise _scheduling_locked()
    return order_lock_service.unlock_order(db, order_id, current_user)


@router.patch("/{order_id}/soft-pin", response_model=SoftPinResponse)
async def set_soft_pin(
    order_id: uuid.UUID,
    request: SoftPinRequest,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(_WRITE_ROLES),
) -> SoftPinResponse:
    """Set a preferred production date (soft pin) for an order.

    The scheduling engine will try to honour this date but may move the order
    if capacity is insufficient.

    Permission: scheduler+.

    Errors:
        404: order not found.
    """
    if await is_scheduling_locked(redis):
        raise _scheduling_locked()
    return order_lock_service.set_soft_pin(db, order_id, request.preferred_date, current_user)


@router.delete("/{order_id}/soft-pin", response_model=SoftPinResponse)
async def clear_soft_pin(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(_WRITE_ROLES),
) -> SoftPinResponse:
    """Clear the soft-pin preferred date from an order.

    Permission: scheduler+.

    Errors:
        404: order not found.
    """
    if await is_scheduling_locked(redis):
        raise _scheduling_locked()
    return order_lock_service.clear_soft_pin(db, order_id, current_user)
