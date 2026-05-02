"""AuditLog entity — persists a DB-queryable record of every CRUD event."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base_class import Base


class AuditLog(Base):
    """Immutable record of a state-changing operation on a domain resource."""

    __tablename__ = "audit_logs"

    action: Mapped[str] = mapped_column(
        sa.String(64),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    resource_type: Mapped[str] = mapped_column(
        sa.String(64),
        nullable=False,
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    old_value: Mapped[dict | None] = mapped_column(  # type: ignore[type-arg]
        JSONB,
        nullable=True,
    )
    new_value: Mapped[dict | None] = mapped_column(  # type: ignore[type-arg]
        JSONB,
        nullable=True,
    )
