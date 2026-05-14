"""Pydantic DTOs for the order domain."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.order import OrderStatus

__all__ = [
    "AuditLogResponse",
    "BatchUpdateRequest",
    "BatchUpdateResponse",
    "CreateOrderRequest",
    "OrderListResponse",
    "OrderResponse",
    "OrderStatus",
    "UpdateOrderRequest",
]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class CreateOrderRequest(BaseModel):
    """Payload for POST /orders (scheduler+)."""

    customer_name: str = Field(..., min_length=1, max_length=255)
    wafer_quantity: int = Field(..., ge=25, le=2500)
    requested_delivery_date: date
    assigned_to: uuid.UUID | None = None
    notes: str | None = None


class UpdateOrderRequest(BaseModel):
    """Payload for PATCH /orders/{order_id} (scheduler+).

    `version_id` is required for optimistic-lock validation.
    `assigned_to` uses model_fields_set as sentinel: omitting the field keeps
    the current assignee; sending null clears it; sending a UUID reassigns.
    Only scheduler and root may change assigned_to.
    """

    wafer_quantity: int | None = Field(default=None, ge=25, le=2500)
    requested_delivery_date: date | None = None
    notes: str | None = None
    assigned_to: uuid.UUID | None = Field(default=None, description="Pass null to unassign")
    version_id: int = Field(..., description="Current version_id (optimistic lock)")


class BatchUpdateRequest(BaseModel):
    """Payload for PATCH /orders/batch-update (scheduler+)."""

    order_ids: list[uuid.UUID] = Field(..., min_length=1)
    requested_delivery_date: date


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class OrderResponse(BaseModel):
    """Public view of a single order record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_number: str
    customer_name: str
    wafer_quantity: int
    requested_delivery_date: date
    scheduled_production_date: date | None
    expected_delivery_date: date | None
    status: OrderStatus
    assigned_to: uuid.UUID | None
    created_by: uuid.UUID
    notes: str | None
    # Pin fields (see app/models/order.py for semantic).
    pinned_production_date: date | None
    is_pinned: bool
    is_processing_locked: bool
    version_id: int
    created_at: datetime
    updated_at: datetime


class OrderListResponse(BaseModel):
    """Paginated list of orders."""

    items: list[OrderResponse]
    total: int
    page: int
    page_size: int


class BatchUpdateResponse(BaseModel):
    """Result of a batch delivery-date update."""

    updated_count: int
    skipped_count: int
    skipped_ids: list[uuid.UUID]


class AuditLogResponse(BaseModel):
    """Single audit-log entry for an order."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: str
    user_id: uuid.UUID | None
    resource_id: uuid.UUID
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    created_at: datetime
