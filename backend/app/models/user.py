"""User domain entity."""

from __future__ import annotations

from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base_class import Base


class UserRole(StrEnum):
    """RBAC roles ordered from most to least privileged."""

    root = "root"
    scheduler = "scheduler"
    order_manager = "order_manager"
    viewer = "viewer"


_user_role_enum = sa.Enum(
    UserRole,
    name="userrole",
    create_type=True,
)


class User(Base):
    """Registered system user with role-based access control."""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(
        sa.String(64),
        unique=True,
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(
        sa.String(254),
        unique=True,
        nullable=False,
        index=True,
    )
    password_hash: Mapped[str] = mapped_column(
        sa.String(255),
        nullable=False,
    )
    role: Mapped[UserRole] = mapped_column(
        _user_role_enum,
        nullable=False,
        server_default=UserRole.viewer.value,
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        server_default="true",
    )
