"""JWT generation/validation and RBAC dependency injection.

Importable by any layer that needs to authenticate or authorise a request.
`get_current_user` and `require_roles` are the two public FastAPI dependencies.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.models.user import User, UserRole
from app.repositories import user as user_repo
from app.schemas.user import TokenPayload

# auto_error=False so we can raise 401 (HTTPBearer default is 403 on missing token).
http_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the stored *hashed* password."""
    try:
        return bool(bcrypt.checkpw(plain.encode(), hashed.encode()))
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def create_access_token(user_id: uuid.UUID, role: UserRole) -> str:
    """Sign and return a JWT access token for the given user."""
    settings = get_settings()
    now = datetime.now(tz=UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role.value,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + settings.JWT_ACCESS_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(
        payload,
        settings.JWT_SECRET.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_access_token(token: str) -> TokenPayload:
    """Decode and validate a JWT; raise 401 on any failure."""
    settings = get_settings()
    try:
        raw = jwt.decode(
            token,
            settings.JWT_SECRET.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials.",
        ) from exc
    try:
        return TokenPayload(sub=raw["sub"], role=raw["role"], exp=raw["exp"])
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials.",
        ) from exc


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials.",
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(http_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Validate the bearer token and return the corresponding active User.

    Raises HTTP 401 for any token problem or inactive account.
    """
    if credentials is None:
        raise _UNAUTHORIZED
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = uuid.UUID(payload.sub)
    except ValueError as exc:
        raise _UNAUTHORIZED from exc

    user = user_repo.get_by_id(db, user_id)
    if user is None or not user.is_active:
        raise _UNAUTHORIZED
    return user


def require_roles(*roles: UserRole) -> Callable[..., Coroutine[Any, Any, User]]:
    """Return a FastAPI dependency that enforces role membership.

    Usage::

        @router.post("/orders")
        async def create_order(
            current_user: User = Depends(require_roles(UserRole.order_manager, UserRole.root))
        ):
            ...
    """

    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions.",
            )
        return current_user

    return _check
