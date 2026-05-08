"""Auth API tests covering the login, register, and me endpoints.

Run `pytest tests/api/test_auth.py -v` to execute this test module.
"""

from __future__ import annotations

import bcrypt
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    username: str,
    password: str = "password123",
    role: UserRole = UserRole.viewer,
    is_active: bool = True,
) -> User:
    """Insert a user directly into the DB for test setup."""
    user = User(
        username=username,
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


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------


def test_login_success_returns_token(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="alice", password="secret123")

    res = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "secret123"},
    )

    assert res.status_code == 200
    body = res.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 10


def test_login_wrong_password_returns_401(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="bob", password="correct")

    res = client.post(
        "/api/v1/auth/login",
        json={"username": "bob", "password": "wrong"},
    )

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


def test_login_nonexistent_user_returns_401(client: TestClient) -> None:
    res = client.post(
        "/api/v1/auth/login",
        json={"username": "nobody", "password": "anything"},
    )

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


def test_login_inactive_user_returns_401(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="carol", password="pass1234", is_active=False)

    res = client.post(
        "/api/v1/auth/login",
        json={"username": "carol", "password": "pass1234"},
    )

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


def test_login_missing_fields_returns_422(client: TestClient) -> None:
    res = client.post("/api/v1/auth/login", json={"username": "dave"})

    assert res.status_code == 422
    assert res.json()["error"]["code"] == 422


# ---------------------------------------------------------------------------
# Register tests
# ---------------------------------------------------------------------------


def test_register_without_token_succeeds(client: TestClient) -> None:
    res = client.post(
        "/api/v1/auth/register",
        json={"username": "newuser", "password": "newpassword1", "role": "viewer"},
    )

    assert res.status_code == 201
    body = res.json()
    assert body["username"] == "newuser"
    assert body["role"] == "viewer"
    assert "password_hash" not in body


def test_register_duplicate_username_returns_409(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="existing", password="pass1234")

    res = client.post(
        "/api/v1/auth/register",
        json={"username": "existing", "password": "newpassword1", "role": "viewer"},
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_register_with_root_role_is_forced_to_viewer(client: TestClient) -> None:
    res = client.post(
        "/api/v1/auth/register",
        json={"username": "tricky", "password": "password1", "role": "root"},
    )

    assert res.status_code == 201
    assert res.json()["role"] == "viewer"


# ---------------------------------------------------------------------------
# Me tests
# ---------------------------------------------------------------------------


def test_me_returns_current_user(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="eve", password="mypassword", role=UserRole.scheduler)
    token = _login(client, "eve", "mypassword")

    res = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["username"] == "eve"
    assert body["role"] == "scheduler"
    assert body["is_active"] is True
    assert "id" in body
    assert "created_at" in body
    assert "password_hash" not in body


def test_me_invalid_token_returns_401(client: TestClient) -> None:
    res = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer this.is.not.valid"},
    )

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


def test_me_with_malformed_sub_returns_401(client: TestClient) -> None:
    # Sign a structurally valid JWT but with a non-UUID `sub` to exercise the
    # uuid.UUID(payload.sub) ValueError branch in get_current_user.
    import jwt as pyjwt

    token = pyjwt.encode(
        {"sub": "not-a-uuid", "role": "viewer", "exp": 9999999999},
        "test-secret-do-not-use-in-prod",
        algorithm="HS256",
    )

    res = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


def test_me_with_deactivated_user_returns_401(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="deactivated", password="pass5678")
    token = _login(client, "deactivated", "pass5678")

    user.is_active = False
    db_session.commit()

    res = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


def test_login_with_corrupted_password_hash_returns_401(
    client: TestClient, db_session: Session
) -> None:
    # Covers the except ValueError branch in verify_password():
    # bcrypt.checkpw raises ValueError when the stored hash is not valid bcrypt.
    user = _make_user(db_session, username="corrupted", password="goodpass1")
    user.password_hash = "not-a-valid-hash"
    db_session.commit()

    res = client.post(
        "/api/v1/auth/login",
        json={"username": "corrupted", "password": "goodpass1"},
    )

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


def test_me_with_missing_claims_token_returns_401(client: TestClient) -> None:
    # Covers the except (KeyError, TypeError) branch in decode_access_token():
    # JWT is structurally valid but payload is missing the required "sub" claim.
    import jwt as pyjwt

    token = pyjwt.encode(
        {"role": "viewer", "exp": 9999999999},  # "sub" intentionally omitted
        "test-secret-do-not-use-in-prod",
        algorithm="HS256",
    )

    res = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401
