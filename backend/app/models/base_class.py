"""Declarative Base for the entity layer.

Every domain entity inherits from `Base`, which provides five cross-cutting
columns required by the project's PRD:

  * `id`         — UUIDv4 primary key (collision-free across services).
  * `created_at` — server-side insertion timestamp (UTC).
  * `updated_at` — server-side update timestamp (UTC), bumped on every UPDATE.
  * `is_deleted` — soft-delete flag (queries should filter by `is_deleted=False`).
  * `version_id` — optimistic-lock counter (prevents concurrent overwrites
                   when two users edit the same order — see PRD §1.2).

Optimistic locking is enabled via `__mapper_args__ = {"version_id_col": ...}`
on the Base; SQLAlchemy then auto-increments `version_id` on every flush and
raises `StaleDataError` if another transaction has already incremented it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Root of all domain entities.

    Subclasses should declare only their own columns — `id`, `created_at`,
    `updated_at`, `is_deleted`, and `version_id` are inherited automatically.
    """

    # --- Identity -------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )

    # --- Audit columns --------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # --- Soft delete ----------------------------------------------------------
    # Per PRD §1.6: orders flagged "deleted/cancelled" for >90 days are archived,
    # never hard-deleted. Active queries must filter `is_deleted == False`.
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        nullable=False,
        index=True,
    )

    # --- Optimistic locking ---------------------------------------------------
    # Per PRD §1.2: prevent two managers from silently overwriting each other.
    # On every UPDATE, SQLAlchemy adds `WHERE version_id = <expected>` and
    # raises StaleDataError if zero rows match.
    version_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    # SQLAlchemy looks for the literal name `__mapper_args__`; no annotation is
    # used so we don't fight the parent's typing. `noqa: RUF012` because this
    # is class-level config consumed by SQLAlchemy, not a per-instance default.
    __mapper_args__ = {"version_id_col": version_id}  # noqa: RUF012

    # ------------------------------------------------------------------ helpers
    def __repr__(self) -> str:
        """Compact repr useful in logs and pytest failure messages."""
        return f"<{type(self).__name__} id={self.id} v={self.version_id}>"
