"""Auth business logic — login, register."""

from __future__ import annotations

import structlog
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.logger import audit_log
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.repositories import user as user_repo
from app.schemas.user import LoginRequest, LoginResponse, RegisterRequest, UserResponse

logger = structlog.get_logger(__name__)

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid credentials.",
)


def login(db: Session, request: LoginRequest) -> LoginResponse:
    """Authenticate a user and return a JWT access token.

    Always returns 401 regardless of whether the username exists or the
    password is wrong — prevents account enumeration.
    """
    user = user_repo.get_by_username(db, request.username)
    if user is None or not user.is_active:
        raise _INVALID_CREDENTIALS
    if not verify_password(request.password, user.password_hash):
        raise _INVALID_CREDENTIALS

    token = create_access_token(user.id, user.role)
    logger.info("user.login", username=user.username, user_id=str(user.id))
    return LoginResponse(access_token=token)


def register(db: Session, request: RegisterRequest, actor: User) -> UserResponse:
    """Create a new user account (root only).

    Raises 409 if the username is already taken.
    Emits an audit log entry on success.
    """
    if user_repo.get_by_username(db, request.username) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{request.username}' is already taken.",
        )

    new_user = user_repo.create(
        db,
        username=request.username,
        password_hash=hash_password(request.password),
        role=request.role,
        email=request.email,
    )
    db.commit()

    audit_log(
        action="user.created",
        actor_id=str(actor.id),
        resource_type="user",
        resource_id=str(new_user.id),
        changes={"username": new_user.username, "role": new_user.role.value},
    )

    return UserResponse.model_validate(new_user)
