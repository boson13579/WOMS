"""Order domain entity."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

import sqlalchemy as sa
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

    # --- Locking fields (Task 3) ---
    is_locked: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        server_default="false",
    )
    locked_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("users.id"),
        nullable=True,
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    soft_pin_date: Mapped[date | None] = mapped_column(
        sa.Date,
        nullable=True,
    )
