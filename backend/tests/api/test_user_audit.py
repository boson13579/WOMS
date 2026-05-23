"""Tests for ``GET /api/v1/users/{user_id}/audit`` (PR B3).

The endpoint is root-only and returns a paginated, newest-first list of
audit-log rows whose ``resource_type='user'`` and ``resource_id=user_id``.
Tests pre-seed rows directly via ``audit_log_repo.create`` (rather than
exercising the full update/deactivate flow) so this module remains decoupled
from B1's caller migration.

Run ``pytest tests/api/test_user_audit.py -v`` to execute.
"""

# RED → GREEN sequence: each test was written failing first against an absent
# implementation, then turned green by the code under test. Markers below show
# which assertion line was the original red.

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole
from app.repositories import audit_log as audit_log_repo
from fastapi.testclient import TestClient
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


def _login(client: TestClient, username: str, password: str = "pass1234") -> str:
    """Return a valid access token for the given credentials."""
    res = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200
    return res.json()["access_token"]


def _root_token(client: TestClient, db: Session, *, username: str = "root_audit") -> str:
    """Create a root user and return their access token."""
    _make_user(db, username=username, role=UserRole.root)
    return _login(client, username)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_user_audit(
    db: Session,
    user_id: uuid.UUID,
    *,
    count: int = 1,
    action: str = "user.updated",
    resource_type: str = "user",
    actor_id: uuid.UUID | None = None,
) -> list[AuditLog]:
    """Insert *count* audit rows for *user_id* and commit.

    Each row's ``new_value`` carries the loop index so tests can verify
    ordering by content rather than by ``created_at`` alone. Rows inserted
    in the same SQLAlchemy ``flush()`` collide on the ``func.now()``
    server timestamp, which would make any DESC-by-created_at assertion
    non-deterministic. We override ``created_at`` post-insert with
    monotonically-increasing offsets (seq=0 → earliest, seq=N-1 → newest)
    so the test contract "seq=N-1 is newest" holds regardless of the
    server clock resolution.
    """
    base = datetime.now(UTC) - timedelta(hours=count)
    rows: list[AuditLog] = []
    for i in range(count):
        row = audit_log_repo.create(
            db,
            action=action,
            user_id=actor_id,
            resource_type=resource_type,
            resource_id=user_id,
            new_value={"seq": i},
        )
        # Override the server-generated created_at so DESC ordering is
        # deterministic. seq=0 is oldest, seq=count-1 is newest.
        row.created_at = base + timedelta(seconds=i)
        rows.append(row)
    db.commit()
    return rows


# ---------------------------------------------------------------------------
# RBAC + auth
# ---------------------------------------------------------------------------


def test_get_user_audit_log_root_only(client: TestClient, db_session: Session) -> None:
    """Non-root callers must get 403, regardless of which non-root role."""
    target = _make_user(db_session, username="audit_target_403")
    for role, username in [
        (UserRole.viewer, "viewer_audit"),
        (UserRole.order_manager, "om_audit"),
        (UserRole.scheduler, "sched_audit"),
    ]:
        _make_user(db_session, username=username, role=role)
        token = _login(client, username)

        res = client.get(f"/api/v1/users/{target.id}/audit", headers=_auth(token))

        # RED: route did not exist → 404 returned instead of 403 for non-root roles.
        assert res.status_code == 403, f"role={role} should be forbidden"
        assert res.json()["error"]["code"] == 403


def test_get_user_audit_log_unauthenticated_returns_401(
    client: TestClient, db_session: Session
) -> None:
    target = _make_user(db_session, username="audit_target_401")

    res = client.get(f"/api/v1/users/{target.id}/audit")

    # RED: route did not exist → 404 returned instead of 401 for missing auth.
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# 404 + empty cases
# ---------------------------------------------------------------------------


def test_get_user_audit_log_user_not_found(client: TestClient, db_session: Session) -> None:
    """A random UUID with no matching user must return 404, not 200-empty.

    Diverges from ``/orders/{id}/audit-log`` (which 404s only via
    ``get_by_id_including_deleted``). Chosen so admins don't get false-
    positive empty lists for typo'd IDs.
    """
    token = _root_token(client, db_session)
    bogus = uuid.uuid4()

    res = client.get(f"/api/v1/users/{bogus}/audit", headers=_auth(token))

    # RED: pre-impl service returned 200 + empty list for unknown user_id instead of 404.
    assert res.status_code == 404
    assert res.json()["error"]["code"] == 404


def test_get_user_audit_log_empty(client: TestClient, db_session: Session) -> None:
    """A user with no audit rows must return 200 + empty items + total=0."""
    token = _root_token(client, db_session)
    target = _make_user(db_session, username="audit_target_empty")

    res = client.get(f"/api/v1/users/{target.id}/audit", headers=_auth(token))

    # RED: empty-case response shape (items/total/page/page_size envelope) did not exist yet.
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["page"] == 1
    assert body["page_size"] == 20


# ---------------------------------------------------------------------------
# Returned-data + ordering
# ---------------------------------------------------------------------------


def test_get_user_audit_log_returns_entries_desc(client: TestClient, db_session: Session) -> None:
    """Insert 5 rows; response must be sorted by created_at DESC (newest first).

    We assert order via the ``new_value['seq']`` we stamped at seed time so
    the test stays deterministic even when created_at timestamps collide at
    microsecond resolution.
    """
    token = _root_token(client, db_session)
    target = _make_user(db_session, username="audit_target_desc")
    _seed_user_audit(db_session, target.id, count=5, action="user.updated")

    res = client.get(f"/api/v1/users/{target.id}/audit", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 5
    assert len(body["items"]) == 5

    # The seed loop inserted rows with seq=0..4 in ascending created_at;
    # newest-first means seq=4 should come first.
    seqs = [item["new_value"]["seq"] for item in body["items"]]
    # RED: repo's default ascending order returned [0,1,2,3,4] before DESC + id-tiebreaker landed.
    assert seqs == [4, 3, 2, 1, 0]

    # And each item carries the expected resource_id + action.
    for item in body["items"]:
        assert item["resource_id"] == str(target.id)
        assert item["action"] == "user.updated"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_get_user_audit_log_pagination(client: TestClient, db_session: Session) -> None:
    """30 rows + page=2&page_size=10 → 10 items, total=30, page reflects input."""
    token = _root_token(client, db_session)
    target = _make_user(db_session, username="audit_target_pagn")
    _seed_user_audit(db_session, target.id, count=30, action="user.updated")

    # Page 1 — newest 10 rows (seq 20..29 inserted last, so DESC gives 29..20).
    res = client.get(
        f"/api/v1/users/{target.id}/audit?page=1&page_size=10",
        headers=_auth(token),
    )
    # RED: repo lacked offset/limit support; whole 30-row list came back regardless of page/page_size.
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 30
    assert body["page"] == 1
    assert body["page_size"] == 10
    assert len(body["items"]) == 10
    seqs_page1 = [item["new_value"]["seq"] for item in body["items"]]
    assert seqs_page1 == list(range(29, 19, -1))

    # Page 2 — next 10 (seq 19..10).
    res = client.get(
        f"/api/v1/users/{target.id}/audit?page=2&page_size=10",
        headers=_auth(token),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 30
    assert body["page"] == 2
    assert body["page_size"] == 10
    assert len(body["items"]) == 10
    seqs_page2 = [item["new_value"]["seq"] for item in body["items"]]
    assert seqs_page2 == list(range(19, 9, -1))

    # Page 3 — final 10 (seq 9..0).
    res = client.get(
        f"/api/v1/users/{target.id}/audit?page=3&page_size=10",
        headers=_auth(token),
    )
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) == 10
    seqs_page3 = [item["new_value"]["seq"] for item in body["items"]]
    assert seqs_page3 == list(range(9, -1, -1))


# ---------------------------------------------------------------------------
# resource_type filter
# ---------------------------------------------------------------------------


def test_get_user_audit_log_filters_resource_type(client: TestClient, db_session: Session) -> None:
    """Audit rows with ``resource_id=user_id`` but ``resource_type='order'``
    must not leak into the response — endpoint filters on both columns.
    """
    token = _root_token(client, db_session)
    target = _make_user(db_session, username="audit_target_rt")

    # Two ``user`` rows + one cross-domain ``order`` row sharing the same
    # resource_id (defensive against UUID collision).
    _seed_user_audit(db_session, target.id, count=2, action="user.updated", resource_type="user")
    audit_log_repo.create(
        db_session,
        action="order.created",
        user_id=None,
        resource_type="order",
        resource_id=target.id,
        new_value={"customer_name": "noise"},
    )
    db_session.commit()

    res = client.get(f"/api/v1/users/{target.id}/audit", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    # RED: pre-impl repo filtered only by resource_id and returned the order row too → total == 3.
    assert body["total"] == 2
    for item in body["items"]:
        assert item["action"] == "user.updated"


# ---------------------------------------------------------------------------
# Deactivated user
# ---------------------------------------------------------------------------


def test_get_user_audit_log_deactivated_user_still_visible(
    client: TestClient, db_session: Session
) -> None:
    """Audit history of a deactivated user must still be returned.

    Admins must be able to review the history of accounts they have
    disabled (e.g. for an HR / security investigation). Plain
    ``user_repo.get_by_id`` already returns inactive rows; this test
    pins the behaviour.
    """
    token = _root_token(client, db_session)
    target = _make_user(
        db_session,
        username="audit_target_inactive",
        is_active=False,
    )
    _seed_user_audit(db_session, target.id, count=3, action="user.deactivated")

    res = client.get(f"/api/v1/users/{target.id}/audit", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    # RED: early service draft filtered out is_active=False targets → empty list returned.
    assert body["total"] == 3
    assert len(body["items"]) == 3


# ---------------------------------------------------------------------------
# Root viewing another root
# ---------------------------------------------------------------------------


def test_get_user_audit_log_root_viewing_another_root(
    client: TestClient, db_session: Session
) -> None:
    """Verifier-requested explicit coverage: one root viewing another root's
    audit history must succeed (no self-only restriction at this layer).
    """
    _make_user(db_session, username="root_A", role=UserRole.root)
    target_root = _make_user(db_session, username="root_B", role=UserRole.root)
    token = _login(client, "root_A")
    _seed_user_audit(db_session, target_root.id, count=2, action="user.updated")

    res = client.get(f"/api/v1/users/{target_root.id}/audit", headers=_auth(token))

    # RED: an early self-only RBAC variant 403'd root_A looking at root_B before _root_only landed.
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


# ---------------------------------------------------------------------------
# Pagination query-param validation
# ---------------------------------------------------------------------------


def test_get_user_audit_log_invalid_page_returns_422(
    client: TestClient, db_session: Session
) -> None:
    """``page=0`` violates ``ge=1`` and must return 422 from FastAPI."""
    token = _root_token(client, db_session)
    target = _make_user(db_session, username="audit_target_p_invalid")

    res = client.get(
        f"/api/v1/users/{target.id}/audit?page=0",
        headers=_auth(token),
    )
    # RED: without Query(ge=1) the endpoint accepted page=0 and returned 200 with off-by-one offset.
    assert res.status_code == 422


def test_get_user_audit_log_page_size_capped_at_100(
    client: TestClient, db_session: Session
) -> None:
    """``page_size=101`` must be rejected by ``Query(..., le=100)``."""
    token = _root_token(client, db_session)
    target = _make_user(db_session, username="audit_target_ps_invalid")

    res = client.get(
        f"/api/v1/users/{target.id}/audit?page_size=101",
        headers=_auth(token),
    )
    # RED: without Query(le=100) the endpoint accepted page_size=101 and returned 200.
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Response shape sanity
# ---------------------------------------------------------------------------


def test_get_user_audit_log_item_shape(client: TestClient, db_session: Session) -> None:
    """Each item must expose the AuditLogResponse fields contractually."""
    token = _root_token(client, db_session)
    actor = _make_user(db_session, username="audit_actor_shape", role=UserRole.root)
    target = _make_user(db_session, username="audit_target_shape")
    before = datetime.now(UTC)
    audit_log_repo.create(
        db_session,
        action="user.updated",
        user_id=actor.id,
        resource_type="user",
        resource_id=target.id,
        old_value={"username": "old"},
        new_value={"username": "new"},
    )
    db_session.commit()

    res = client.get(f"/api/v1/users/{target.id}/audit", headers=_auth(token))
    assert res.status_code == 200
    item = res.json()["items"][0]

    # RED: AuditLogResponse lived in schemas/order with a slimmer field set → keys() was missing user_id/resource_id.
    assert set(item.keys()) >= {
        "id",
        "action",
        "user_id",
        "resource_id",
        "old_value",
        "new_value",
        "created_at",
    }
    assert item["action"] == "user.updated"
    assert item["user_id"] == str(actor.id)
    assert item["resource_id"] == str(target.id)
    assert item["old_value"] == {"username": "old"}
    assert item["new_value"] == {"username": "new"}
    # created_at parseable and within 1 minute of seed time
    parsed = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    assert abs((parsed - before).total_seconds()) < 60
    assert datetime.now(UTC) - parsed < timedelta(minutes=1)
