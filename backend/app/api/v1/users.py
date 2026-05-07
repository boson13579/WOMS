"""User CRUD HTTP router — root-only management endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_roles
from app.models.user import User, UserRole
from app.schemas.user import UserListResponse, UserResponse, UserUpdateRequest
from app.services import user as user_service

router = APIRouter()

_root_only = Depends(require_roles(UserRole.root))


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
    if search:
        return user_service.search_users(db, search)
    return user_service.list_users(db)


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
