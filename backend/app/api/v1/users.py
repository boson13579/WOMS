"""User CRUD HTTP router — root-only management endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user, require_roles
from app.models.user import User, UserRole
from app.schemas.user import (
    AssignableUserResponse,
    UserListResponse,
    UserResponse,
    UserSelfUpdateRequest,
    UserUpdateRequest,
)
from app.services import user as user_service

router = APIRouter()

_root_only = Depends(require_roles(UserRole.root))
_any_auth = Depends(get_current_user)


@router.get("", response_model=UserListResponse)
def list_users(
    search: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = _root_only,
) -> UserListResponse:
    """List all users, optionally filtered by ?search= (username or email).

    Permission: root only.

    Errors:
        401: missing or invalid bearer token.
        403: authenticated user does not have the root role.
    """
    return user_service.list_users(db, search=search)


@router.get("/assignable", response_model=list[AssignableUserResponse])
def get_assignable_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_roles(UserRole.order_manager, UserRole.scheduler, UserRole.root)
    ),
) -> list[AssignableUserResponse]:
    """Return the list of users that can be assigned as order owners.

    Permission: order_manager+. order_manager sees only themselves;
    scheduler and root see all active users.

    Errors:
        401: missing or invalid bearer token.
        403: authenticated user does not have at least order_manager role.
    """
    return user_service.get_assignable_users(db, current_user)


@router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = _root_only,
) -> UserResponse:
    """Return a single user by id.

    Permission: root only.

    Errors:
        401: missing or invalid bearer token.
        403: authenticated user does not have the root role.
        404: user not found.
    """
    return user_service.get_user(db, user_id)


@router.patch("/me", response_model=UserResponse)
def update_self(
    request: UserSelfUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = _any_auth,
) -> UserResponse:
    """Update the calling user's own username or email.

    Cannot change role or is_active.  Requires version_id for optimistic locking.

    Permission: any authenticated user.

    Errors:
        401: missing or invalid bearer token.
        409: version_id mismatch or duplicate username/email.
        422: request body fails validation.
    """
    return user_service.update_self(db, current_user, request)


@router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: uuid.UUID,
    request: UserUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = _root_only,
) -> UserResponse:
    """Partially update a user (username, email, role, is_active).

    Requires version_id for optimistic locking.

    Permission: root only.

    Errors:
        401: missing or invalid bearer token.
        403: authenticated user does not have the root role.
        404: user not found.
        409: version_id mismatch, duplicate username, or last-root protection.
        422: request body fails validation.
    """
    return user_service.update_user(db, user_id, request, current_user)


@router.delete("/{user_id}", response_model=UserResponse)
def deactivate_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = _root_only,
) -> UserResponse:
    """Deactivate a user (sets is_active=False). Row is retained in DB.

    Idempotent — returns 200 even if already inactive.

    Permission: root only.

    Errors:
        401: missing or invalid bearer token.
        403: authenticated user does not have the root role.
        404: user not found.
        409: last-root protection triggered.
    """
    return user_service.deactivate_user(db, user_id, current_user)
