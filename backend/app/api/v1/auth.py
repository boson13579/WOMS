"""Auth HTTP router — login, register, me."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user, require_roles
from app.models.user import User, UserRole
from app.schemas.user import LoginRequest, LoginResponse, RegisterRequest, UserResponse
from app.services import auth as auth_service

router = APIRouter()


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    """Authenticate with username/password and return a JWT bearer token.

    Permission: public — no token required.

    Errors:
        401: credentials invalid, account inactive, or account not found
             (reason is intentionally not distinguished to prevent enumeration).
        422: request body missing required fields.
    """
    return auth_service.login(db, request)


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    request: RegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.root)),
) -> UserResponse:
    """Create a new user account and return the created user profile.

    Permission: root only — bearer token with role=root required.

    Errors:
        401: missing or invalid bearer token.
        403: authenticated user does not have the root role.
        409: username is already taken.
        422: request body fails validation (e.g. password shorter than 8 chars).
    """
    return auth_service.register(db, request, current_user)


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """Return the profile of the currently authenticated user.

    Permission: any authenticated user — valid bearer token required.

    Errors:
        401: missing, expired, structurally invalid token, or account deactivated.
    """
    return UserResponse.model_validate(current_user)
