"""Notification entity — persists user-facing event notifications."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base_class import Base


class Notification(Base):
    """A notification record created when an order event occurs."""

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("orders.id"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(
        sa.String(50),
        nullable=False,
    )
    message: Mapped[str] = mapped_column(
        sa.Text,
        nullable=False,
    )
    is_read: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
