"""User repository — pure CRUD, no business logic."""

from __future__ import annotations

import uuid

from sqlalchemy import select
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
