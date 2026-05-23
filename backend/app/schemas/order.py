"""Pydantic DTOs for the order domain."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.order import OrderStatus

# Re-exported for backward compatibility. The canonical home for the
# audit-log DTO is now ``app.schemas.audit`` (it's cross-resource, not
# order-specific); callers may still import it from this module without
# breakage. New code should prefer the canonical path.
from app.schemas.audit import AuditLogResponse

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

    Three fields use ``model_fields_set`` as a "client sent this on purpose"
    sentinel — needed because their ``None`` value is a legal user input
    (= "clear it") and Pydantic's default ``None`` would otherwise be
    indistinguishable from "omitted":

    - ``assigned_to`` — null = unassign; missing = keep current
    - ``notes`` — null = clear note; missing = keep current
    - ``pinned_production_date`` — null = unpin the order; missing = keep
      current pin state; date value = pin to that production day
      (or change pin day if already pinned)

    Only scheduler and root may change assigned_to.
    """

    wafer_quantity: int | None = Field(default=None, ge=25, le=2500)
    requested_delivery_date: date | None = None
    notes: str | None = None
    assigned_to: uuid.UUID | None = Field(default=None, description="Pass null to unassign")
    pinned_production_date: date | None = Field(
        default=None,
        description=(
            "Pin the order to a specific production day. Pass null to unpin, "
            "omit the field to keep the current pin state."
        ),
    )
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
