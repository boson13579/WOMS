"""Tests for GET /api/v1/users/assignable.

Run `pytest tests/api/test_assignable_users.py -v` to execute this module.
"""

from __future__ import annotations

import bcrypt
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASSWORD = "pass1234"


def _make_user(
    db: Session,
    *,
    username: str,
    role: UserRole = UserRole.viewer,
    is_active: bool = True,
    is_deleted: bool = False,
    email: str | None = None,
) -> User:
    user = User(
        username=username,
        email=email or f"{username}@test.internal",
        password_hash=bcrypt.hashpw(_PASSWORD.encode(), bcrypt.gensalt()).decode(),
        role=role,
        is_active=is_active,
        is_deleted=is_deleted,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client: TestClient, username: str) -> str:
    res = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert res.status_code == 200
    return res.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_assignable_returns_all_active_users_for_scheduler(
    client: TestClient, db_session: Session
) -> None:
    """Scheduler calling /assignable receives all is_active=True users."""
    scheduler = _make_user(db_session, username="sched_a", role=UserRole.scheduler)
    _make_user(db_session, username="other_a", role=UserRole.order_manager)
    token = _login(client, "sched_a")

    res = client.get("/api/v1/users/assignable", headers=_auth(token))

    assert res.status_code == 200
    ids = {item["id"] for item in res.json()}
    assert str(scheduler.id) in ids
    assert len(res.json()) >= 2


def test_assignable_returns_only_self_for_order_manager(
    client: TestClient, db_session: Session
) -> None:
    """order_manager calling /assignable only receives themselves."""
    manager = _make_user(db_session, username="mgr_a", role=UserRole.order_manager)
    _make_user(db_session, username="other_b", role=UserRole.scheduler)
    token = _login(client, "mgr_a")

    res = client.get("/api/v1/users/assignable", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["id"] == str(manager.id)


def test_assignable_returns_all_for_root(client: TestClient, db_session: Session) -> None:
    """root calling /assignable receives all is_active=True users."""
    root = _make_user(db_session, username="root_a", role=UserRole.root)
    _make_user(db_session, username="other_c", role=UserRole.scheduler)
    token = _login(client, "root_a")

    res = client.get("/api/v1/users/assignable", headers=_auth(token))

    assert res.status_code == 200
    ids = {item["id"] for item in res.json()}
    assert str(root.id) in ids
    assert len(res.json()) >= 2


def test_assignable_excludes_inactive_users(client: TestClient, db_session: Session) -> None:
    """Inactive users must not appear in the assignable list."""
    _make_user(db_session, username="sched_b", role=UserRole.scheduler)
    inactive = _make_user(
        db_session, username="inactive_a", role=UserRole.order_manager, is_active=False
    )
    token = _login(client, "sched_b")

    res = client.get("/api/v1/users/assignable", headers=_auth(token))

    assert res.status_code == 200
    ids = {item["id"] for item in res.json()}
    assert str(inactive.id) not in ids


def test_assignable_excludes_deleted_users(client: TestClient, db_session: Session) -> None:
    """Soft-deleted users must not appear in the assignable list."""
    _make_user(db_session, username="sched_d", role=UserRole.scheduler)
    deleted = _make_user(
        db_session, username="deleted_a", role=UserRole.order_manager, is_deleted=True
    )
    token = _login(client, "sched_d")

    res = client.get("/api/v1/users/assignable", headers=_auth(token))

    assert res.status_code == 200
    ids = {item["id"] for item in res.json()}
    assert str(deleted.id) not in ids


def test_assignable_viewer_returns_403(client: TestClient, db_session: Session) -> None:
    """viewer role calling /assignable is forbidden (HTTP 403)."""
    _make_user(db_session, username="viewer_a", role=UserRole.viewer)
    token = _login(client, "viewer_a")

    res = client.get("/api/v1/users/assignable", headers=_auth(token))

    assert res.status_code == 403


def test_assignable_requires_auth(client: TestClient, db_session: Session) -> None:
    """Calling /assignable without a token returns 401."""
    res = client.get("/api/v1/users/assignable")
    assert res.status_code == 401


def test_assignable_response_contains_id_username_email(
    client: TestClient, db_session: Session
) -> None:
    """Each item in the response contains exactly id, username, and email."""
    _make_user(db_session, username="sched_c", role=UserRole.scheduler)
    token = _login(client, "sched_c")

    res = client.get("/api/v1/users/assignable", headers=_auth(token))

    assert res.status_code == 200
    for item in res.json():
        assert set(item.keys()) == {"id", "username", "email"}
