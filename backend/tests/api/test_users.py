"""User CRUD API tests — root-only endpoints.

Run `pytest tests/api/test_users.py -v` to execute this module.
"""

from __future__ import annotations

from unittest.mock import patch

import bcrypt
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    username: str,
    password: str = "pass1234",
    role: UserRole = UserRole.viewer,
    is_active: bool = True,
    email: str | None = None,
) -> User:
    """Insert a user directly into the DB for test setup."""
    user = User(
        username=username,
        email=email or f"{username}@test.internal",
        password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        role=role,
        is_active=is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client: TestClient, username: str, password: str) -> str:
    """Return a valid access token for the given credentials."""
    res = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200
    return res.json()["access_token"]


def _root_headers(client: TestClient, db: Session) -> dict[str, str]:
    """Create a root user and return auth headers."""
    _make_user(db, username="root_user", password="rootpass1", role=UserRole.root)
    token = _login(client, "root_user", "rootpass1")
    return {"Authorization": f"Bearer {token}"}


def _non_root_headers(client: TestClient, db: Session) -> dict[str, str]:
    """Create a viewer user and return auth headers."""
    _make_user(db, username="viewer_user", password="viewpass1", role=UserRole.viewer)
    token = _login(client, "viewer_user", "viewpass1")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# GET /users — list
# ---------------------------------------------------------------------------


def test_list_users_as_root_returns_200(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    _make_user(db_session, username="alice")

    res = client.get("/api/v1/users", headers=headers)

    assert res.status_code == 200
    body = res.json()
    assert "users" in body
    assert "total" in body
    assert body["total"] >= 1


def test_list_users_search_by_username(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    _make_user(db_session, username="searchable_bob")
    _make_user(db_session, username="other_charlie")

    res = client.get("/api/v1/users?search=searchable", headers=headers)

    assert res.status_code == 200
    body = res.json()
    usernames = [u["username"] for u in body["users"]]
    assert "searchable_bob" in usernames
    assert "other_charlie" not in usernames


def test_list_users_search_by_email(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    _make_user(db_session, username="diana", email="diana@example.com")
    _make_user(db_session, username="evan", email="evan@other.org")

    res = client.get("/api/v1/users?search=diana@", headers=headers)

    assert res.status_code == 200
    body = res.json()
    usernames = [u["username"] for u in body["users"]]
    assert "diana" in usernames
    assert "evan" not in usernames


def test_list_users_as_non_root_returns_403(client: TestClient, db_session: Session) -> None:
    headers = _non_root_headers(client, db_session)

    res = client.get("/api/v1/users", headers=headers)

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_list_users_without_token_returns_401(client: TestClient) -> None:
    res = client.get("/api/v1/users")

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# GET /users/{user_id}
# ---------------------------------------------------------------------------


def test_get_user_by_id_success(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="frank")

    res = client.get(f"/api/v1/users/{target.id}", headers=headers)

    assert res.status_code == 200
    body = res.json()
    assert body["username"] == "frank"
    assert body["id"] == str(target.id)
    assert "version_id" in body


def test_get_user_not_found_returns_404(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)

    res = client.get(
        "/api/v1/users/00000000-0000-0000-0000-000000000000",
        headers=headers,
    )

    assert res.status_code == 404
    assert res.json()["error"]["code"] == 404


def test_get_user_without_token_returns_401(client: TestClient, db_session: Session) -> None:
    target = _make_user(db_session, username="grace")

    res = client.get(f"/api/v1/users/{target.id}")

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# PATCH /users/{user_id}
# ---------------------------------------------------------------------------


def test_patch_user_update_role_success(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="henry", role=UserRole.viewer)

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"role": "scheduler", "version_id": target.version_id},
        headers=headers,
    )

    assert res.status_code == 200
    body = res.json()
    assert body["role"] == "scheduler"


def test_patch_user_update_username_success(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="iris_old")

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"username": "iris_new", "version_id": target.version_id},
        headers=headers,
    )

    assert res.status_code == 200
    body = res.json()
    assert body["username"] == "iris_new"


def test_patch_user_stale_version_returns_409(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="jack")

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"role": "scheduler", "version_id": 9999},
        headers=headers,
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_patch_user_duplicate_username_returns_409(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    _make_user(db_session, username="kate_existing")
    target = _make_user(db_session, username="leo")

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"username": "kate_existing", "version_id": target.version_id},
        headers=headers,
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_patch_user_as_non_root_returns_403(client: TestClient, db_session: Session) -> None:
    headers = _non_root_headers(client, db_session)
    target = _make_user(db_session, username="mia")

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"role": "scheduler", "version_id": target.version_id},
        headers=headers,
    )

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_patch_user_without_token_returns_401(client: TestClient, db_session: Session) -> None:
    target = _make_user(db_session, username="noah")

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"role": "scheduler", "version_id": target.version_id},
    )

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# DELETE /users/{user_id}
# ---------------------------------------------------------------------------


def test_delete_user_deactivates_success(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="olivia")

    res = client.delete(f"/api/v1/users/{target.id}", headers=headers)

    assert res.status_code == 200
    body = res.json()
    assert body["is_active"] is False
    # Row must still exist in DB
    db_session.refresh(target)
    assert target.is_active is False
    assert target.is_deleted is False


def test_delete_user_idempotent(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="peter")

    res1 = client.delete(f"/api/v1/users/{target.id}", headers=headers)
    res2 = client.delete(f"/api/v1/users/{target.id}", headers=headers)

    assert res1.status_code == 200
    assert res2.status_code == 200


def test_delete_user_not_found_returns_404(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)

    res = client.delete(
        "/api/v1/users/00000000-0000-0000-0000-000000000000",
        headers=headers,
    )

    assert res.status_code == 404
    assert res.json()["error"]["code"] == 404


def test_delete_user_as_non_root_returns_403(client: TestClient, db_session: Session) -> None:
    headers = _non_root_headers(client, db_session)
    target = _make_user(db_session, username="quinn")

    res = client.delete(f"/api/v1/users/{target.id}", headers=headers)

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_delete_user_without_token_returns_401(client: TestClient, db_session: Session) -> None:
    target = _make_user(db_session, username="rachel")

    res = client.delete(f"/api/v1/users/{target.id}")

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# Last-root protection
# ---------------------------------------------------------------------------


def test_patch_user_clear_email_with_null_returns_422(
    client: TestClient, db_session: Session
) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="sam", email="sam@example.com")

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"email": None, "version_id": target.version_id},
        headers=headers,
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == 422


def test_patch_user_omit_email_leaves_it_unchanged(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="tina", email="tina@example.com")

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"role": "scheduler", "version_id": target.version_id},
        headers=headers,
    )

    assert res.status_code == 200
    assert res.json()["email"] == "tina@example.com"


def test_patch_user_demote_last_root_returns_409(client: TestClient, db_session: Session) -> None:
    root = _make_user(db_session, username="only_root", role=UserRole.root)
    token = _login(client, "only_root", "pass1234")
    headers = {"Authorization": f"Bearer {token}"}

    res = client.patch(
        f"/api/v1/users/{root.id}",
        json={"role": "viewer", "version_id": root.version_id},
        headers=headers,
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_patch_user_deactivate_last_root_returns_409(
    client: TestClient, db_session: Session
) -> None:
    root = _make_user(db_session, username="only_root", role=UserRole.root)
    token = _login(client, "only_root", "pass1234")
    headers = {"Authorization": f"Bearer {token}"}

    res = client.patch(
        f"/api/v1/users/{root.id}",
        json={"is_active": False, "version_id": root.version_id},
        headers=headers,
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_patch_user_demote_root_with_backup_root_succeeds(
    client: TestClient, db_session: Session
) -> None:
    root1 = _make_user(db_session, username="root_one", role=UserRole.root)
    _make_user(db_session, username="root_two", role=UserRole.root)
    token = _login(client, "root_one", "pass1234")
    headers = {"Authorization": f"Bearer {token}"}

    res = client.patch(
        f"/api/v1/users/{root1.id}",
        json={"role": "viewer", "version_id": root1.version_id},
        headers=headers,
    )

    assert res.status_code == 200
    assert res.json()["role"] == "viewer"


def test_delete_user_deactivate_last_root_returns_409(
    client: TestClient, db_session: Session
) -> None:
    root = _make_user(db_session, username="only_root", role=UserRole.root)
    token = _login(client, "only_root", "pass1234")
    headers = {"Authorization": f"Bearer {token}"}

    res = client.delete(f"/api/v1/users/{root.id}", headers=headers)

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


# ---------------------------------------------------------------------------
# [RED] PATCH /users/me — self-update (any authenticated role)
# ---------------------------------------------------------------------------


def test_self_update_username_success(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="self_upd_u", password="password123")
    token = _login(client, "self_upd_u", "password123")

    res = client.patch(
        "/api/v1/users/me",
        json={"username": "self_upd_u_new", "version_id": user.version_id},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    assert res.json()["username"] == "self_upd_u_new"


def test_self_update_email_success(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="self_upd_e", password="password123")
    token = _login(client, "self_upd_e", "password123")

    res = client.patch(
        "/api/v1/users/me",
        json={"email": "updated@example.com", "version_id": user.version_id},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    assert res.json()["email"] == "updated@example.com"


def test_self_update_stale_version_returns_409(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="self_upd_stale", password="password123")
    token = _login(client, "self_upd_stale", "password123")

    res = client.patch(
        "/api/v1/users/me",
        json={"username": "wont_matter", "version_id": 9999},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_self_update_duplicate_username_returns_409(
    client: TestClient, db_session: Session
) -> None:
    _make_user(db_session, username="taken_name")
    user = _make_user(db_session, username="self_upd_dup", password="password123")
    token = _login(client, "self_upd_dup", "password123")

    res = client.patch(
        "/api/v1/users/me",
        json={"username": "taken_name", "version_id": user.version_id},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_self_update_cannot_change_role(client: TestClient, db_session: Session) -> None:
    user = _make_user(
        db_session, username="self_upd_role", role=UserRole.viewer, password="password123"
    )
    token = _login(client, "self_upd_role", "password123")

    res = client.patch(
        "/api/v1/users/me",
        json={"role": "root", "version_id": user.version_id},
        headers={"Authorization": f"Bearer {token}"},
    )

    # role field is not part of UserSelfUpdateRequest → 422 validation error
    assert res.status_code == 422
    assert res.json()["error"]["code"] == 422


def test_self_update_duplicate_email_returns_409(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="taken_email_owner", email="taken@example.com")
    user = _make_user(db_session, username="self_upd_email_dup", password="password123")
    token = _login(client, "self_upd_email_dup", "password123")

    res = client.patch(
        "/api/v1/users/me",
        json={"email": "taken@example.com", "version_id": user.version_id},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


# ---------------------------------------------------------------------------
# Additional PATCH /users/{user_id} edge cases
# ---------------------------------------------------------------------------


def test_patch_user_not_found_returns_404(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)

    res = client.patch(
        "/api/v1/users/00000000-0000-0000-0000-000000000000",
        json={"username": "ghost", "version_id": 1},
        headers=headers,
    )

    assert res.status_code == 404
    assert res.json()["error"]["code"] == 404


def test_patch_user_duplicate_email_returns_409(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    _make_user(db_session, username="email_owner", email="occupied@example.com")
    target = _make_user(db_session, username="email_target")

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"email": "occupied@example.com", "version_id": target.version_id},
        headers=headers,
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_patch_user_reactivate_success(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="reactivate_me")

    # Deactivate first
    client.delete(f"/api/v1/users/{target.id}", headers=headers)
    db_session.refresh(target)

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"is_active": True, "version_id": target.version_id},
        headers=headers,
    )

    assert res.status_code == 200
    assert res.json()["is_active"] is True


def test_patch_root_username_guard_skips_gracefully(
    client: TestClient, db_session: Session
) -> None:
    root = _make_user(db_session, username="root_rename", role=UserRole.root)
    token = _login(client, "root_rename", "pass1234")
    headers = {"Authorization": f"Bearer {token}"}

    res = client.patch(
        f"/api/v1/users/{root.id}",
        json={"username": "root_rename_new", "version_id": root.version_id},
        headers=headers,
    )

    assert res.status_code == 200
    assert res.json()["username"] == "root_rename_new"


def test_patch_inactive_root_role_allowed(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="active_root_guard", role=UserRole.root)
    inactive_root = _make_user(
        db_session, username="inactive_root_guard", role=UserRole.root, is_active=False
    )
    token = _login(client, "active_root_guard", "pass1234")
    headers = {"Authorization": f"Bearer {token}"}

    res = client.patch(
        f"/api/v1/users/{inactive_root.id}",
        json={"role": "scheduler", "version_id": inactive_root.version_id},
        headers=headers,
    )

    assert res.status_code == 200
    assert res.json()["role"] == "scheduler"


def test_patch_user_update_email_success(client: TestClient, db_session: Session) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="email_update_target")

    res = client.patch(
        f"/api/v1/users/{target.id}",
        json={"email": "brand_new@example.com", "version_id": target.version_id},
        headers=headers,
    )

    assert res.status_code == 200
    assert res.json()["email"] == "brand_new@example.com"



# ---------------------------------------------------------------------------
# IntegrityError race-condition guards (mock-based)
# ---------------------------------------------------------------------------


def test_self_update_duplicate_email_integrity_error_returns_409(
    client: TestClient, db_session: Session
) -> None:
    user = _make_user(db_session, username="self_upd_ie_email", password="password123")
    token = _login(client, "self_upd_ie_email", "password123")

    fake_orig = Exception('duplicate key value violates unique constraint "ix_users_email"')
    integrity_err = IntegrityError("stmt", {}, fake_orig)

    with patch("app.services.user.user_repo.update_self", side_effect=integrity_err):
        res = client.patch(
            "/api/v1/users/me",
            json={"email": "race@example.com", "version_id": user.version_id},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_admin_update_user_duplicate_email_returns_409(
    client: TestClient, db_session: Session
) -> None:
    headers = _root_headers(client, db_session)
    target = _make_user(db_session, username="ie_email_target")

    fake_orig = Exception('duplicate key value violates unique constraint "ix_users_email"')
    integrity_err = IntegrityError("stmt", {}, fake_orig)

    with patch("app.services.user.user_repo.update", side_effect=integrity_err):
        res = client.patch(
            f"/api/v1/users/{target.id}",
            json={"email": "race@example.com", "version_id": target.version_id},
            headers=headers,
        )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409
