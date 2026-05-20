"""Tests for ``GET /api/v1/audit/events`` — PR A3 global audit feed.

Endpoint is root-only and returns a paginated, newest-first slice of the
``audit_logs`` table with optional actor / action / resource_type /
resource_id / created_at range filters. Tests pre-seed rows directly via
``audit_log_repo.create`` (rather than exercising the full update flow)
so this module remains decoupled from any single business workflow —
the global feed must work no matter which write path produced the rows.

Run ``pytest tests/api/test_audit_events.py -v`` to execute.
"""

# RED → GREEN sequence: each test was written failing first against an absent
# implementation, then turned green by the code under test. Markers below show
# which assertion line was the original red.

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

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


def _root_token(client: TestClient, db: Session, *, username: str = "root_events") -> str:
    """Create a root user and return their access token."""
    _make_user(db, username=username, role=UserRole.root)
    return _login(client, username)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_events(
    db: Session,
    *,
    count: int,
    action: str = "user.updated",
    resource_type: str = "user",
    resource_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    base_time: datetime | None = None,
    seq_offset: int = 0,
) -> list[AuditLog]:
    """Seed *count* audit rows, returning them in insertion (seq) order.

    Each row's ``created_at`` is forced to ``base_time + seq * 1s`` so
    that DESC-by-created_at assertions are deterministic — the bulk
    ``func.now()`` server timestamp would otherwise collapse to one
    value per ``flush()`` and the page-boundary tests would be flaky.

    ``new_value['seq']`` carries the loop index so tests can assert by
    content rather than relying on UUID equality.
    """
    rid = resource_id if resource_id is not None else uuid.uuid4()
    base = base_time if base_time is not None else datetime.now(UTC) - timedelta(hours=count)
    rows: list[AuditLog] = []
    for i in range(count):
        row = audit_log_repo.create(
            db,
            action=action,
            user_id=actor_id,
            resource_type=resource_type,
            resource_id=rid,
            new_value={"seq": seq_offset + i},
        )
        row.created_at = base + timedelta(seconds=i)
        rows.append(row)
    db.commit()
    return rows


def _seqs(items: Sequence[dict[str, object]]) -> list[int]:
    """Pull the ``new_value['seq']`` field out of a response item list."""
    out: list[int] = []
    for item in items:
        new_value = item["new_value"]
        assert isinstance(new_value, dict), "new_value should be a dict"
        out.append(int(new_value["seq"]))
    return out


# ---------------------------------------------------------------------------
# 1. RBAC — root only
# ---------------------------------------------------------------------------


def test_audit_events_root_only(client: TestClient, db_session: Session) -> None:
    """viewer / order_manager / scheduler → 403; root → 200.

    Audit history is sensitive (carries PII + pre-mutation snapshots),
    so even scheduler is too broad an audience. Only root may read.
    """
    for role, username in [
        (UserRole.viewer, "viewer_events"),
        (UserRole.order_manager, "om_events"),
        (UserRole.scheduler, "sched_events"),
    ]:
        _make_user(db_session, username=username, role=role)
        token = _login(client, username)

        res = client.get("/api/v1/audit/events", headers=_auth(token))

        # RED: route did not exist → 404 returned instead of 403 for non-root roles.
        assert res.status_code == 403, f"role={role} should be forbidden"
        assert res.json()["error"]["code"] == 403

    # Root succeeds on the same endpoint.
    root_token = _root_token(client, db_session)
    res = client.get("/api/v1/audit/events", headers=_auth(root_token))
    assert res.status_code == 200, "root should be allowed"


# ---------------------------------------------------------------------------
# 2. Pagination — total + page slice
# ---------------------------------------------------------------------------


def test_audit_events_returns_paginated(client: TestClient, db_session: Session) -> None:
    """Seed 30 rows → page=2 page_size=10 → 10 items, total=30, page reflected.

    Same pagination contract as ``GET /users/{id}/audit`` — items + total
    + page + page_size envelope so the FE paginator can compute page count.

    Filters by ``action=user.updated`` to scope away the
    ``user.login_succeeded`` row that the root login writes — otherwise
    ``total`` would be 31 instead of the 30 we seeded.
    """
    token = _root_token(client, db_session)
    _seed_events(db_session, count=30, action="user.updated")

    res = client.get(
        "/api/v1/audit/events?page=2&page_size=10&action=user.updated",
        headers=_auth(token),
    )

    # RED: pre-impl response didn't carry total / page / page_size, just items.
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 30
    assert body["page"] == 2
    assert body["page_size"] == 10
    assert len(body["items"]) == 10


# ---------------------------------------------------------------------------
# 3. Ordering — created_at DESC (newest first)
# ---------------------------------------------------------------------------


def test_audit_events_orders_by_created_at_desc(
    client: TestClient,
    db_session: Session,
) -> None:
    """Default order is newest-first; seq=N-1 (last inserted) comes first.

    Scopes to ``action=user.updated`` to exclude the login row that the
    test fixture writes via ``_root_token`` — otherwise the login row
    would sort newest (its ``created_at`` is "now") and break the
    deterministic seq=[4,3,2,1,0] assertion.
    """
    token = _root_token(client, db_session)
    _seed_events(db_session, count=5, action="user.updated")

    res = client.get(
        "/api/v1/audit/events?action=user.updated",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    # RED: a naive list_events without explicit DESC would return [0,1,2,3,4].
    assert _seqs(body["items"]) == [4, 3, 2, 1, 0]


# ---------------------------------------------------------------------------
# 4. Filter — actor_id
# ---------------------------------------------------------------------------


def test_audit_events_filter_by_actor_id(client: TestClient, db_session: Session) -> None:
    """Seed rows for two actors; filtering returns only the chosen actor's rows."""
    token = _root_token(client, db_session)
    alice = _make_user(db_session, username="alice_actor", role=UserRole.order_manager)
    bob = _make_user(db_session, username="bob_actor", role=UserRole.order_manager)
    _seed_events(db_session, count=3, action="user.updated", actor_id=alice.id, seq_offset=0)
    _seed_events(db_session, count=2, action="user.updated", actor_id=bob.id, seq_offset=100)

    res = client.get(
        f"/api/v1/audit/events?actor_id={alice.id}",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    # RED: missing actor_id filter would return all 5 rows.
    assert body["total"] == 3
    assert len(body["items"]) == 3
    for item in body["items"]:
        assert item["user_id"] == str(alice.id)


# ---------------------------------------------------------------------------
# 5. Filter — action (exact match)
# ---------------------------------------------------------------------------


def test_audit_events_filter_by_action(client: TestClient, db_session: Session) -> None:
    """Mixed actions; ``?action=user.deactivated`` returns only that subset.

    Seeds with ``user.deactivated`` rather than ``user.login_succeeded``
    so the filter doesn't accidentally pick up the login row that the
    ``_root_token`` fixture writes.
    """
    token = _root_token(client, db_session)
    _seed_events(db_session, count=4, action="user.deactivated", seq_offset=0)
    _seed_events(db_session, count=3, action="user.updated", seq_offset=100)
    _seed_events(db_session, count=2, action="order.created", resource_type="order", seq_offset=200)

    res = client.get(
        "/api/v1/audit/events?action=user.deactivated",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    # RED: missing action filter would return all 9 rows.
    assert body["total"] == 4
    for item in body["items"]:
        assert item["action"] == "user.deactivated"


# ---------------------------------------------------------------------------
# 6. Filter — resource_type
# ---------------------------------------------------------------------------


def test_audit_events_filter_by_resource_type(client: TestClient, db_session: Session) -> None:
    """Mixed user / order resource_types; filter narrows to one type."""
    token = _root_token(client, db_session)
    _seed_events(db_session, count=3, action="user.updated", resource_type="user", seq_offset=0)
    _seed_events(
        db_session,
        count=4,
        action="order.created",
        resource_type="order",
        seq_offset=100,
    )

    res = client.get(
        "/api/v1/audit/events?resource_type=order",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    # RED: missing resource_type filter would return 7 rows mixed.
    assert body["total"] == 4
    seqs = _seqs(body["items"])
    assert all(s >= 100 for s in seqs), f"order rows should have seq >= 100, got {seqs}"


# ---------------------------------------------------------------------------
# 7. Filter — resource_id
# ---------------------------------------------------------------------------


def test_audit_events_filter_by_resource_id(client: TestClient, db_session: Session) -> None:
    """Filter by a specific UUID returns only rows touching that resource."""
    token = _root_token(client, db_session)
    rid_a = uuid.uuid4()
    rid_b = uuid.uuid4()
    _seed_events(
        db_session,
        count=3,
        action="user.updated",
        resource_id=rid_a,
        seq_offset=0,
    )
    _seed_events(
        db_session,
        count=2,
        action="user.updated",
        resource_id=rid_b,
        seq_offset=100,
    )

    res = client.get(
        f"/api/v1/audit/events?resource_id={rid_a}",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    # RED: missing resource_id filter would return all 5 rows.
    assert body["total"] == 3
    for item in body["items"]:
        assert item["resource_id"] == str(rid_a)


# ---------------------------------------------------------------------------
# 8. Filter — date range (from / to)
# ---------------------------------------------------------------------------


def test_audit_events_filter_by_date_range(client: TestClient, db_session: Session) -> None:
    """``?from=X&to=Y`` returns only rows with X <= created_at < Y.

    Interval is half-open [from, to) — same convention as the repository,
    avoids the "is the boundary second included?" question.
    """
    token = _root_token(client, db_session)

    # 9 rows: 3 BEFORE the window, 3 INSIDE, 3 AFTER. Spread is 100s
    # between groups so the window can comfortably bracket the middle
    # third with timestamps that aren't adjacent.
    anchor = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    _seed_events(
        db_session,
        count=3,
        action="user.updated",
        base_time=anchor - timedelta(seconds=300),
        seq_offset=0,
    )
    _seed_events(
        db_session,
        count=3,
        action="user.updated",
        base_time=anchor,
        seq_offset=100,
    )
    _seed_events(
        db_session,
        count=3,
        action="user.updated",
        base_time=anchor + timedelta(seconds=300),
        seq_offset=200,
    )

    # Half-open window [anchor, anchor+10s) should capture only the
    # middle group (which is seeded at anchor + 0s, +1s, +2s).
    # URL-encode the ISO-8601 timestamps because the trailing ``+00:00``
    # offset would otherwise be decoded as a literal space and the
    # query-string layer would 422 on the malformed datetime.
    window_from = quote(anchor.isoformat())
    window_to = quote((anchor + timedelta(seconds=10)).isoformat())

    res = client.get(
        f"/api/v1/audit/events?from={window_from}&to={window_to}",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    # RED: missing from/to filters would return all 9 rows.
    assert body["total"] == 3
    seqs = _seqs(body["items"])
    assert all(100 <= s < 103 for s in seqs), f"window should select middle group, got {seqs}"


# ---------------------------------------------------------------------------
# 9. Combined filters — actor AND action
# ---------------------------------------------------------------------------


def test_audit_events_combined_filters(client: TestClient, db_session: Session) -> None:
    """``?actor_id=A&action=X`` is AND-combined: only rows matching BOTH."""
    token = _root_token(client, db_session)
    actor_a = _make_user(db_session, username="combo_a", role=UserRole.order_manager)
    actor_b = _make_user(db_session, username="combo_b", role=UserRole.order_manager)

    # actor_a / login → 2 rows (target group)
    _seed_events(
        db_session,
        count=2,
        action="user.login_succeeded",
        actor_id=actor_a.id,
        seq_offset=0,
    )
    # actor_a / updated → 3 rows (same actor, wrong action)
    _seed_events(
        db_session,
        count=3,
        action="user.updated",
        actor_id=actor_a.id,
        seq_offset=10,
    )
    # actor_b / login → 4 rows (right action, wrong actor)
    _seed_events(
        db_session,
        count=4,
        action="user.login_succeeded",
        actor_id=actor_b.id,
        seq_offset=20,
    )

    res = client.get(
        f"/api/v1/audit/events?actor_id={actor_a.id}&action=user.login_succeeded",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    # RED: an OR-combined filter would return 2+3+4=9 rows; AND must return only 2.
    assert body["total"] == 2
    for item in body["items"]:
        assert item["user_id"] == str(actor_a.id)
        assert item["action"] == "user.login_succeeded"


# ---------------------------------------------------------------------------
# 10. Empty filters — everything paginated
# ---------------------------------------------------------------------------


def test_audit_events_empty_filters_returns_all(
    client: TestClient,
    db_session: Session,
) -> None:
    """No filter args → return everything (paginated to default 20).

    The ``_root_token`` fixture writes one ``user.login_succeeded`` row as
    a side effect of authenticating, so the unfiltered total is 25 seeded
    + 1 login = 26. We assert against the full unfiltered count to lock
    in the "no filters means every row" contract.
    """
    token = _root_token(client, db_session)
    _seed_events(db_session, count=25, action="user.updated")

    res = client.get("/api/v1/audit/events", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    # RED: an incomplete filter wiring might have only returned filtered subsets.
    # 25 seeded + 1 login_succeeded from the _root_token fixture.
    assert body["total"] == 26
    # Default page_size is 20, so first page has 20 of the 26.
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert len(body["items"]) == 20


# ---------------------------------------------------------------------------
# 11. Pagination bounds — Query(...) validators
# ---------------------------------------------------------------------------


def test_audit_events_pagination_bounds(client: TestClient, db_session: Session) -> None:
    """``page < 1`` and ``page_size > 100`` must be rejected with 422.

    Pinned via ``Query(ge=1)`` / ``Query(le=100)`` so FastAPI rejects
    bad pagination knobs at the binding layer before the route runs.
    """
    token = _root_token(client, db_session)

    # page=0 violates ge=1.
    res = client.get("/api/v1/audit/events?page=0", headers=_auth(token))
    # RED: without Query(ge=1) the endpoint accepts page=0 and silently
    # uses offset=-page_size (or floors to 0) instead of 422.
    assert res.status_code == 422

    # page_size=101 violates le=100.
    res = client.get("/api/v1/audit/events?page_size=101", headers=_auth(token))
    # RED: without Query(le=100) the endpoint accepts page_size=101 and
    # serves an unbounded page instead of 422.
    assert res.status_code == 422

    # page_size=0 violates ge=1.
    res = client.get("/api/v1/audit/events?page_size=0", headers=_auth(token))
    assert res.status_code == 422
