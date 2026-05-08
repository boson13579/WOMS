"""User CRUD business logic — list, get, update, deactivate."""

from __future__ import annotations

import uuid

import structlog
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.logger import audit_log
from app.models.user import User, UserRole
from app.repositories import audit_log as audit_log_repo
from app.repositories import user as user_repo
from app.schemas.user import UserListResponse, UserResponse, UserUpdateRequest

logger = structlog.get_logger(__name__)

_LAST_ROOT_MSG = "Cannot demote/deactivate the last active root user."


def _guard_last_root(
    db: Session, user: User, new_role: UserRole | None, new_is_active: bool | None
) -> None:
    """Raise 409 if the operation would leave no active root user."""
    if user.role != UserRole.root:
        return
    if not user.is_active:
        return
    will_demote = new_role is not None and new_role != UserRole.root
    will_deactivate = new_is_active is False
    if not will_demote and not will_deactivate:
        return
    if user_repo.count_active_roots_excluding(db, user.id) == 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_LAST_ROOT_MSG)


def list_users(db: Session, search: str | None = None) -> UserListResponse:
    """Return all non-deleted users, optionally filtered by *search*."""
    users = user_repo.list_users(db, search=search)
    return UserListResponse(
        users=[UserResponse.model_validate(u) for u in users],
        total=len(users),
    )


def get_user(db: Session, user_id: uuid.UUID) -> UserResponse:
    """Return a single user by id; raise 404 if not found."""
    user = user_repo.get_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return UserResponse.model_validate(user)


def update_user(
    db: Session,
    user_id: uuid.UUID,
    request: UserUpdateRequest,
    actor: User,
) -> UserResponse:
    """Apply partial updates to a user; enforce optimistic lock and last-root protection."""
    user = user_repo.get_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if user.version_id != request.version_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User was modified by another request. Refresh and try again.",
        )

    if request.username is not None:
        existing = user_repo.get_by_username(db, request.username)
        if existing is not None and existing.id != user.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Username '{request.username}' is already taken.",
            )

    _guard_last_root(db, user, request.role, request.is_active)

    old_val = {
        "username": user.username,
        "email": user.email,
        "role": user.role.value,
        "is_active": user.is_active,
    }
    new_val: dict[str, object] = {}

    try:
        user_repo.update(
            db,
            user,
            fields_set=request.model_fields_set,
            username=request.username,
            email=request.email,
            role=request.role,
            is_active=request.is_active,
        )
        new_val = {
            "username": user.username,
            "email": user.email,
            "role": user.role.value,
            "is_active": user.is_active,
        }
        audit_log_repo.create(
            db,
            action="user.updated",
            user_id=actor.id,
            resource_type="user",
            resource_id=user.id,
            old_value=old_val,
            new_value=new_val,
        )
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User was modified by another request. Refresh and try again.",
        ) from exc

    audit_log(
        action="user.updated",
        actor_id=str(actor.id),
        resource_type="user",
        resource_id=str(user.id),
        changes={"old": old_val, "new": new_val},
    )

    return UserResponse.model_validate(user)


def deactivate_user(db: Session, user_id: uuid.UUID, actor: User) -> UserResponse:
    """Set is_active=False (soft-deactivate); idempotent if already inactive."""
    user = user_repo.get_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if not user.is_active:
        return UserResponse.model_validate(user)

    _guard_last_root(db, user, None, False)

    try:
        user_repo.deactivate(db, user)
        audit_log_repo.create(
            db,
            action="user.deactivated",
            user_id=actor.id,
            resource_type="user",
            resource_id=user.id,
            old_value={"is_active": True},
            new_value={"is_active": False},
        )
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User was modified concurrently. Please retry.",
        ) from exc

    audit_log(
        action="user.deactivated",
        actor_id=str(actor.id),
        resource_type="user",
        resource_id=str(user.id),
        changes={"old": {"is_active": True}, "new": {"is_active": False}},
    )

    return UserResponse.model_validate(user)
