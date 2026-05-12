"""Order domain entity."""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base_class import Base


class OrderStatus(StrEnum):
    """Lifecycle states of a wafer order."""

    pending = "pending"
    scheduled = "scheduled"
    in_production = "in_production"
    completed = "completed"
    cancelled = "cancelled"


_order_status_enum = sa.Enum(
    OrderStatus,
    name="orderstatus",
    create_type=True,
)

# Statuses that allow mutation via PATCH
MUTABLE_STATUSES: frozenset[OrderStatus] = frozenset({OrderStatus.pending, OrderStatus.scheduled})


class Order(Base):
    """A wafer production order."""

    __tablename__ = "orders"

    __table_args__ = (
        sa.CheckConstraint(
            "wafer_quantity >= 25 AND wafer_quantity <= 2500",
            name="ck_orders_wafer_quantity",
        ),
        sa.Index("ix_orders_status_assigned_to", "status", "assigned_to"),
        sa.Index("ix_orders_scheduled_production_date", "scheduled_production_date"),
    )

    order_number: Mapped[str] = mapped_column(
        sa.String(32),
        unique=True,
        nullable=False,
        index=True,
    )
    customer_name: Mapped[str] = mapped_column(
        sa.String(255),
        nullable=False,
    )
    wafer_quantity: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
    )
    requested_delivery_date: Mapped[date] = mapped_column(
        sa.Date,
        nullable=False,
    )
    scheduled_production_date: Mapped[date | None] = mapped_column(
        sa.Date,
        nullable=True,
    )
    expected_delivery_date: Mapped[date | None] = mapped_column(
        sa.Date,
        nullable=True,
    )
    status: Mapped[OrderStatus] = mapped_column(
        _order_status_enum,
        nullable=False,
        server_default=OrderStatus.pending.value,
    )
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("users.id"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("users.id"),
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(
        sa.Text,
        nullable=True,
    )

    # ----- Pin fields ------------------------------------------------------
    # Two independent flags, both spec'd in docs/scheduling.md §pinning:
    #
    # ``is_pinned`` + ``pinned_production_date`` form the *production pin*: a
    # user-requested forced production day (must be ≤ requested_delivery_date).
    # When the scheduler accepts the request, it stores the pin day here and
    # treats the order as fixed-on-that-day in segment trees / compute_schedule.
    # ``pinned_production_date`` is null whenever ``is_pinned`` is false.
    #
    # ``is_processing_locked`` is the *editing-lock pin*: set true while an op
    # for this order is in flight in the scheduler queue, cleared once the
    # worker has applied it. The frontend treats it as "do not let the user
    # edit this row right now" — a UX hint, not a hard authorization gate.
    pinned_production_date: Mapped[date | None] = mapped_column(
        sa.Date,
        nullable=True,
    )
    is_pinned: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )
    is_processing_locked: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    )

    # Per-day production split, materialized by ``materialize_schedule_task``.
    # Shape: ``[{"date": "2026-05-12", "quantity": 6000}, ...]`` sorted by
    # date ascending. NULL when the order isn't currently scheduled (same
    # semantic as ``scheduled_production_date IS NULL``).
    # ``GET /schedule/result`` reads this directly instead of recomputing
    # the breakdown from the live Redis state — the materializer's job is
    # to keep this column in sync with whatever ``compute_schedule`` would
    # have produced for the current state.
    daily_breakdown: Mapped[list[dict[str, str | int]] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
