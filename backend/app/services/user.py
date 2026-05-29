"""User CRUD business logic — list, get, update, deactivate."""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.audit import record_audit
from app.models.user import User, UserRole
from app.repositories import audit_log as audit_log_repo
from app.repositories import user as user_repo
from app.schemas.audit import AuditLogResponse, UserAuditLogListResponse
from app.schemas.user import (
    AssignableUserResponse,
    UserListResponse,
    UserResponse,
    UserSelfUpdateRequest,
    UserUpdateRequest,
)

_LAST_ROOT_MSG = "Cannot demote/deactivate the last active root user."
_USER_NOT_FOUND_MSG = "User not found."
_USER_STALE_VERSION_MSG = "User was modified by another request. Refresh and try again."


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
    if user_repo.lock_and_count_other_active_roots(db, user.id) == 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_LAST_ROOT_MSG)


def get_assignable_users(db: Session, current_user: User) -> list[AssignableUserResponse]:
    """Return the list of users that can be assigned as order owners.

    Business rules:
    - root / scheduler: all active users.
    - order_manager: only themselves.
    """
    if current_user.role == UserRole.order_manager:
        return [AssignableUserResponse.model_validate(current_user)]
    users = user_repo.list_active_users(db)
    return [AssignableUserResponse.model_validate(u) for u in users]


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_USER_NOT_FOUND_MSG)
    return UserResponse.model_validate(user)


def _check_self_username_conflict(db: Session, current_user: User, username: str | None) -> None:
    if username is None:
        return
    existing = user_repo.get_by_username(db, username)
    if existing is not None and existing.id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{username}' is already taken.",
        )


def _check_self_email_conflict(db: Session, current_user: User, email: str | None) -> None:
    if email is None:
        return
    existing_email = user_repo.get_by_email(db, email)
    if existing_email is not None and existing_email.id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email '{email}' is already in use.",
        )


def _raise_unique_conflict(
    exc: IntegrityError, *, email: str | None, username: str | None
) -> NoReturn:
    """Translate an IntegrityError into a 409 with the right message."""
    orig = str(exc.orig).lower()
    if "ix_users_email" in orig or "users_email" in orig:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email '{email or '<unknown>'}' is already in use.",
        ) from exc
    if "ix_users_username" in orig or "users_username" in orig:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{username or '<unknown>'}' is already taken.",
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Conflict: username or email already taken.",
    ) from exc


def update_self(
    db: Session,
    current_user: User,
    request: UserSelfUpdateRequest,
) -> UserResponse:
    """Let any authenticated user update their own username / email."""
    if current_user.version_id != request.version_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_USER_STALE_VERSION_MSG,
        )

    _check_self_username_conflict(db, current_user, request.username)

    if "email" in request.model_fields_set and request.email is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="email cannot be set to null; omit the field to leave it unchanged.",
        )

    _check_self_email_conflict(db, current_user, request.email)

    old_val = {"username": current_user.username, "email": current_user.email}
    new_val: dict[str, object] = {}

    try:
        user_repo.update_self(
            db,
            current_user,
            fields_set=request.model_fields_set,
            username=request.username,
            email=request.email,
        )
        new_val = {"username": current_user.username, "email": current_user.email}
        record_audit(
            db,
            action="user.self_updated",
            actor_id=current_user.id,
            resource_type="user",
            resource_id=current_user.id,
            old_value=old_val,
            new_value=new_val,
        )
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_USER_STALE_VERSION_MSG,
        ) from exc
    except IntegrityError as exc:
        db.rollback()
        _raise_unique_conflict(exc, email=request.email, username=request.username)

    return UserResponse.model_validate(current_user)


def _check_user_username_conflict(db: Session, user: User, username: str | None) -> None:
    if username is None:
        return
    existing = user_repo.get_by_username(db, username)
    if existing is not None and existing.id != user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{username}' is already taken.",
        )


def _check_user_email_conflict(db: Session, user: User, email: str | None) -> None:
    if email is None:
        return
    existing_email = user_repo.get_by_email(db, email)
    if existing_email is not None and existing_email.id != user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email '{email}' is already in use.",
        )


def _validate_update_user_request(db: Session, user: User, request: UserUpdateRequest) -> None:
    """Pre-write validation for ``update_user``: version, email-null, conflicts, last-root."""
    if user.version_id != request.version_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_USER_STALE_VERSION_MSG,
        )

    if "email" in request.model_fields_set and request.email is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="email cannot be set to null; omit the field to leave it unchanged.",
        )

    _check_user_username_conflict(db, user, request.username)
    _check_user_email_conflict(db, user, request.email)
    _guard_last_root(db, user, request.role, request.is_active)


def update_user(
    db: Session,
    user_id: uuid.UUID,
    request: UserUpdateRequest,
    actor: User,
) -> UserResponse:
    """Apply partial updates to a user; enforce optimistic lock and last-root protection."""
    user = user_repo.get_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_USER_NOT_FOUND_MSG)

    _validate_update_user_request(db, user, request)

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
        record_audit(
            db,
            action="user.updated",
            actor_id=actor.id,
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
            detail=_USER_STALE_VERSION_MSG,
        ) from exc
    except IntegrityError as exc:
        db.rollback()
        _raise_unique_conflict(exc, email=request.email, username=request.username)

    return UserResponse.model_validate(user)


def deactivate_user(db: Session, user_id: uuid.UUID, actor: User) -> UserResponse:
    """Set is_active=False (soft-deactivate); idempotent if already inactive."""
    user = user_repo.get_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_USER_NOT_FOUND_MSG)

    if not user.is_active:
        return UserResponse.model_validate(user)

    _guard_last_root(db, user, None, False)

    try:
        user_repo.deactivate(db, user)
        record_audit(
            db,
            action="user.deactivated",
            actor_id=actor.id,
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

    return UserResponse.model_validate(user)


def get_user_audit_log(
    db: Session,
    user_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
) -> UserAuditLogListResponse:
    """Return the paginated audit-log history for *user_id*.

    Behaviour:
      * 404 if no such user exists. Deactivated (``is_active=False``) users
        intentionally remain visible — root must be able to review the
        history of accounts they have already disabled.
      * Filters on ``resource_type='user'`` in addition to ``resource_id``
        so a hypothetical UUID collision between a user-id and an order-id
        cannot cross-contaminate the response.
      * Sorts newest-first (most useful UX for an admin audit view).
      * Returns a paginated wrapper (``items`` / ``total`` / ``page`` /
        ``page_size``) — same shape as ``OrderListResponse`` so the FE can
        reuse its existing paginator widget.
    """
    user = user_repo.get_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_USER_NOT_FOUND_MSG)

    logs = audit_log_repo.get_by_resource_id(
        db,
        user_id,
        resource_type="user",
        page=page,
        page_size=page_size,
        oldest_first=False,
    )
    total = audit_log_repo.count_by_resource_id(
        db,
        user_id,
        resource_type="user",
    )
    return UserAuditLogListResponse(
        items=[AuditLogResponse.model_validate(log) for log in logs],
        total=total,
        page=page,
        page_size=page_size,
    )
