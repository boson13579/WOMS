"""User repository — pure CRUD, no business logic."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.user import User, UserRole


def get_by_username(db: Session, username: str) -> User | None:
    """Return the User with the given username, or None."""
    stmt = select(User).where(User.username == username, User.is_deleted.is_(False))
    return db.scalars(stmt).first()


def get_by_id(db: Session, user_id: uuid.UUID) -> User | None:
    """Return the User with the given primary key, or None."""
    stmt = select(User).where(User.id == user_id, User.is_deleted.is_(False))
    return db.scalars(stmt).first()


def list_users(db: Session, *, search: str | None = None) -> list[User]:
    """Return all non-deleted users, optionally filtered by a search term.

    When *search* is provided the query matches rows where username OR email
    contains the term (case-insensitive).  Results are ordered newest-first.
    """
    stmt = select(User).where(User.is_deleted.is_(False))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                User.username.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    stmt = stmt.order_by(User.created_at.desc())
    return list(db.scalars(stmt).all())


def count_active_roots_excluding(db: Session, exclude_id: uuid.UUID) -> int:
    """Count active root users, excluding the given user_id."""
    stmt = select(func.count()).where(
        User.role == UserRole.root,
        User.is_active.is_(True),
        User.is_deleted.is_(False),
        User.id != exclude_id,
    )
    return db.scalar(stmt) or 0


def create(
    db: Session,
    *,
    username: str,
    password_hash: str,
    role: UserRole,
    email: str | None = None,
) -> User:
    """Insert a new User row and return the persisted instance."""
    user = User(
        username=username,
        password_hash=password_hash,
        role=role,
        email=email,
    )
    db.add(user)
    db.flush()
    db.refresh(user)
    return user


def update(
    db: Session,
    user: User,
    *,
    username: str | None = None,
    email: str | None = None,
    role: UserRole | None = None,
    is_active: bool | None = None,
    **_extra: Any,
) -> User:
    """Apply partial updates to *user* and flush.  Caller must commit."""
    if username is not None:
        user.username = username
    if email is not None:
        user.email = email
    if role is not None:
        user.role = role
    if is_active is not None:
        user.is_active = is_active
    db.flush()
    db.refresh(user)
    return user


def deactivate(db: Session, user: User) -> User:
    """Set is_active=False and flush.  Caller must commit."""
    user.is_active = False
    db.flush()
    db.refresh(user)
    return user
