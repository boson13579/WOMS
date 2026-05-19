"""Auth business logic — login, register."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import UserRole
from app.repositories import user as user_repo
from app.schemas.user import LoginRequest, LoginResponse, RegisterRequest, UserResponse

logger = structlog.get_logger("auth")

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid credentials.",
)


def login(db: Session, request: LoginRequest) -> LoginResponse:
    """Authenticate a user and return a JWT access token.

    Always returns 401 regardless of whether the username exists or the
    password is wrong — prevents account enumeration.

    On success the user row's ``last_login_at`` is stamped to "now" and a
    ``user.login_succeeded`` row is appended to ``audit_logs``. Both writes
    flow through the same SQLAlchemy session as a single commit. If the
    audit step itself raises a non-SAVEPOINT-recoverable error, login still
    returns the token (login availability outranks audit completeness — the
    user already passed credential verification), the warning is logged for
    later forensic recovery, and the ``last_login_at`` stamp is rolled back
    along with the audit row so the snapshot reflects only persisted state.
    """
    user = user_repo.get_by_username(db, request.username)
    if user is None or not user.is_active:
        raise _INVALID_CREDENTIALS
    if not verify_password(request.password, user.password_hash):
        raise _INVALID_CREDENTIALS

    token = create_access_token(user.id, user.role)

    # Stamp last_login_at on the user row (best-effort, same txn as audit).
    user.last_login_at = datetime.now(UTC)

    # Dual-write audit (DB row + structured stdout) via the B1 helper. The
    # helper internally wraps the DB insert in a SAVEPOINT so a transient
    # audit-row failure cannot poison this session. The outer try/except
    # here is belt-and-suspenders: it catches any unexpected error path
    # (e.g. a programmer slip in the helper itself, or a structlog crash)
    # so login still succeeds. We rollback to discard the last_login_at
    # stamp too — the snapshot stays consistent with the audit history.
    try:
        record_audit(
            db,
            action="user.login_succeeded",
            actor_id=user.id,
            resource_type="user",
            resource_id=user.id,
            new_value={"username": user.username},
        )
        db.commit()
    except Exception:
        logger.warning(
            "user.login.audit_failed",
            user_id=str(user.id),
            exc_info=True,
        )
        db.rollback()
    return LoginResponse(access_token=token)


def register(db: Session, request: RegisterRequest) -> UserResponse:
    """Create a new user account (public endpoint).

    Raises 409 if the username is already taken.
    Emits an audit log entry (DB + stdout) on success.
    Actor is the newly created user itself (self-registration).
    """
    if user_repo.get_by_username(db, request.username) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{request.username}' is already taken.",
        )

    if user_repo.get_by_email(db, request.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email '{request.email}' is already in use.",
        )

    try:
        new_user = user_repo.create(
            db,
            username=request.username,
            password_hash=hash_password(request.password),
            role=UserRole.viewer,
            email=request.email,
        )
    except IntegrityError as exc:
        db.rollback()
        orig = str(exc.orig).lower()
        if "ix_users_email" in orig or "users_email" in orig:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Email '{request.email}' is already in use.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{request.username}' is already taken.",
        ) from exc
    record_audit(
        db,
        action="user.created",
        actor_id=new_user.id,
        resource_type="user",
        resource_id=new_user.id,
        new_value={"username": new_user.username, "role": new_user.role.value},
    )
    db.commit()

    return UserResponse.model_validate(new_user)
