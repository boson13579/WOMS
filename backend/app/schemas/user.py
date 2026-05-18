"""Pydantic DTOs for the auth / user domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole

__all__ = [
    "AssignableUserResponse",
    "LoginRequest",
    "LoginResponse",
    "RegisterRequest",
    "TokenPayload",
    "UserListResponse",
    "UserResponse",
    "UserRole",
    "UserSelfUpdateRequest",
    "UserUpdateRequest",
]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Credentials submitted to POST /auth/login."""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class RegisterRequest(BaseModel):
    """Payload for POST /auth/register."""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8)
    email: EmailStr = Field(..., max_length=254)


class UserSelfUpdateRequest(BaseModel):
    """Payload for PATCH /users/me (any authenticated user)."""

    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, min_length=1, max_length=64)
    email: EmailStr | None = Field(default=None, max_length=254)
    version_id: int


class UserUpdateRequest(BaseModel):
    """Payload for PATCH /users/{user_id} (root only)."""

    username: str | None = Field(default=None, min_length=1, max_length=64)
    email: EmailStr | None = Field(default=None, max_length=254)
    role: UserRole | None = None
    is_active: bool | None = None
    version_id: int


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class AssignableUserResponse(BaseModel):
    """Minimal user view returned by GET /users/assignable."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: str | None


class LoginResponse(BaseModel):
    """Returned on successful login."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 — OAuth2 token_type, not a password


class UserResponse(BaseModel):
    """Public view of a user record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: EmailStr
    role: UserRole
    is_active: bool
    version_id: int
    created_at: datetime


class UserListResponse(BaseModel):
    """List of users with the total number of matching records."""

    users: list[UserResponse]
    total: int


# ---------------------------------------------------------------------------
# Internal / token schemas
# ---------------------------------------------------------------------------


class TokenPayload(BaseModel):
    """Claims extracted from a decoded JWT."""

    sub: str  # str(user_id)
    role: str
    exp: int
