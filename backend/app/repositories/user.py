"""User repository — pure CRUD, no business logic."""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
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


def lock_and_count_other_active_roots(db: Session, exclude_id: uuid.UUID) -> int:
    """Lock all active root rows then return the count excluding *exclude_id*.

    SELECT ... FOR UPDATE serialises concurrent last-root checks: the second
    transaction blocks until the first commits, then re-counts under the lock
    and sees the updated state — preventing two simultaneous demote/deactivate
    operations from both believing another root exists.
    """
    stmt = (
        select(User.id)
        .where(
            User.role == UserRole.root,
            User.is_active.is_(True),
            User.is_deleted.is_(False),
        )
        .with_for_update()
    )
    root_ids = list(db.scalars(stmt).all())
    return sum(1 for rid in root_ids if rid != exclude_id)


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
    fields_set: set[str],
    username: str | None = None,
    email: str | None = None,
    role: UserRole | None = None,
    is_active: bool | None = None,
) -> User:
    """Apply partial updates to *user* and flush.  Caller must commit."""
    if "username" in fields_set and username is not None:
        user.username = username
    if "email" in fields_set:
        user.email = email
    if "role" in fields_set and role is not None:
        user.role = role
    if "is_active" in fields_set and is_active is not None:
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
