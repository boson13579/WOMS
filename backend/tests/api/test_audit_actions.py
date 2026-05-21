"""Tests for ``GET /api/v1/audit/actions`` — dynamic Action filter source.

Backs the admin audit page's Action typeahead. The endpoint returns
the distinct, ASC-sorted set of ``action`` strings currently present
in the ``audit_logs`` table. Replaces a hard-coded frontend constant
(``KNOWN_AUDIT_ACTIONS``) that drifted out of sync with reality.

RBAC mirrors ``GET /audit/events`` — root only — so a broader audience
can't use the endpoint as a reconnaissance surface ("which event types
fire in this system?").

Run ``pytest tests/api/test_audit_actions.py -v`` to execute.
"""

from __future__ import annotations

import uuid

import bcrypt
from app.models.user import User, UserRole
from app.repositories import audit_log as audit_log_repo
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers (mirrors test_audit_events.py to keep the modules self-contained)
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


def _login(client: TestClient, username: str, password: str = "pass1234") -> str:
    """Return a valid access token for the given credentials."""
    res = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200
    return res.json()["access_token"]


def _root_token(client: TestClient, db: Session, *, username: str = "root_actions") -> str:
    """Create a root user and return their access token."""
    _make_user(db, username=username, role=UserRole.root)
    return _login(client, username)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_action(db: Session, *, action: str, resource_type: str = "user") -> None:
    """Insert a single audit row with the given action — kept minimal."""
    audit_log_repo.create(
        db,
        action=action,
        user_id=None,
        resource_type=resource_type,
        resource_id=uuid.uuid4(),
        new_value={"marker": action},
    )
    db.commit()


# ---------------------------------------------------------------------------
# 1. RBAC — root only (same gate as /audit/events)
# ---------------------------------------------------------------------------


def test_audit_actions_root_only(client: TestClient, db_session: Session) -> None:
    """viewer / order_manager / scheduler → 403; root → 200.

    The set of distinct actions is itself low-sensitivity, but exposing
    it to a broader audience would give a non-root caller a free
    reconnaissance surface ("which event types fire here?"). Keep the
    gate uniform with ``GET /audit/events``.
    """
    for role, username in [
        (UserRole.viewer, "viewer_actions"),
        (UserRole.order_manager, "om_actions"),
        (UserRole.scheduler, "sched_actions"),
    ]:
        _make_user(db_session, username=username, role=role)
        token = _login(client, username)

        res = client.get("/api/v1/audit/actions", headers=_auth(token))

        assert res.status_code == 403, f"role={role} should be forbidden"
        assert res.json()["error"]["code"] == 403

    # Root succeeds on the same endpoint.
    root_token = _root_token(client, db_session)
    res = client.get("/api/v1/audit/actions", headers=_auth(root_token))
    assert res.status_code == 200, "root should be allowed"


# ---------------------------------------------------------------------------
# 2. Returns distinct + sorted set of actions
# ---------------------------------------------------------------------------


def test_audit_actions_returns_distinct_sorted(
    client: TestClient,
    db_session: Session,
) -> None:
    """Seed mixed + duplicate actions → response is unique and ASC-sorted.

    The DISTINCT clause collapses duplicates and the ``ORDER BY action ASC``
    locks rendering order so the frontend's typeahead stays stable across
    page loads.
    """
    token = _root_token(client, db_session)

    # Mixed actions, with intentional duplicates to verify DISTINCT.
    for action in [
        "user.login_succeeded",
        "order.created",
        "order.created",  # dupe
        "user.update",
        "order.deleted",
        "user.login_succeeded",  # dupe
        "order.scheduled",
    ]:
        _seed_action(db_session, action=action)

    res = client.get("/api/v1/audit/actions", headers=_auth(token))
    assert res.status_code == 200

    body = res.json()
    actions = body["actions"]

    # Unique: 5 distinct values from the 7 seeded rows. The root token
    # fixture's login adds a ``user.login_succeeded`` row, which is
    # already in the seed set, so it doesn't change the distinct count.
    assert len(actions) == len(set(actions)), "result must contain no duplicates"
    assert set(actions) == {
        "user.login_succeeded",
        "order.created",
        "user.update",
        "order.deleted",
        "order.scheduled",
    }

    # ASC-sorted: rely on stable string comparison.
    assert actions == sorted(actions), f"actions must be ASC-sorted, got {actions}"


# ---------------------------------------------------------------------------
# 3. Empty DB → empty list, not error
# ---------------------------------------------------------------------------


def test_audit_actions_returns_empty_on_fresh_db(
    client: TestClient,
    db_session: Session,
) -> None:
    """No audit rows beyond the login one → response is ``{actions: [...]}``,
    not 404 / 500.

    The ``_root_token`` fixture writes one ``user.login_succeeded`` row
    as a side effect of authenticating, so the table isn't strictly
    empty — but the contract under test is "endpoint returns the list,
    even when small/single-entry, rather than treating an empty table
    as an error". We assert that contract directly: 200 with the only
    expected action.
    """
    token = _root_token(client, db_session)

    res = client.get("/api/v1/audit/actions", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    # Only the fixture's login row should be present.
    assert body == {"actions": ["user.login_succeeded"]}
