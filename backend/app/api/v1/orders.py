"""Order CRUD HTTP router.

Route registration order matters: /batch-update must precede /{order_id}
so FastAPI does not interpret the literal string "batch-update" as a UUID.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_roles
from app.models.order import OrderStatus
from app.models.user import User, UserRole
from app.schemas.order import (
    AuditLogResponse,
    BatchUpdateRequest,
    BatchUpdateResponse,
    CreateOrderRequest,
    OrderListResponse,
    OrderResponse,
    UpdateOrderRequest,
)
from app.services import order as order_service

router = APIRouter()

# Roles allowed to read orders (order_manager and above)
_READ_ROLES = require_roles(UserRole.order_manager, UserRole.scheduler, UserRole.root)
# Roles allowed to write orders (scheduler and above)
_WRITE_ROLES = require_roles(UserRole.scheduler, UserRole.root)


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
def update_order(
    order_id: uuid.UUID,
    request: UpdateOrderRequest,
    db: Session = Depends(get_db),
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
    """
    return order_service.update_order(db, order_id, request, current_user)


@router.delete("/{order_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(_WRITE_ROLES),
) -> None:
    """Soft-delete an order: sets is_deleted=True and status=cancelled.

    Permission: scheduler+.

    Errors:
        404: order not found.
    """
    order_service.delete_order(db, order_id, current_user)


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
