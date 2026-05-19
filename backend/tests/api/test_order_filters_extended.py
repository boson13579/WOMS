"""Tests for extended GET /api/v1/orders filters: created_by and multi-value assigned_to.

Run `pytest tests/api/test_order_filters_extended.py -v` to execute this module.
"""

from __future__ import annotations

import uuid
from datetime import date

import bcrypt
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASSWORD = "pass1234"
_DELIVERY = date(2026, 8, 1)


def _make_user(
    db: Session,
    *,
    username: str,
    role: UserRole = UserRole.scheduler,
) -> User:
    user = User(
        username=username,
        email=f"{username}@test.internal",
        password_hash=bcrypt.hashpw(_PASSWORD.encode(), bcrypt.gensalt()).decode(),
        role=role,
        is_active=True,
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


def _make_order(
    db: Session,
    *,
    created_by: uuid.UUID,
    assigned_to: uuid.UUID | None = None,
    customer_name: str = "Test Corp",
    status: OrderStatus = OrderStatus.pending,
) -> Order:
    order = Order(
        order_number=f"ORD-EXT-{uuid.uuid4().hex[:6].upper()}",
        customer_name=customer_name,
        wafer_quantity=100,
        requested_delivery_date=_DELIVERY,
        status=status,
        created_by=created_by,
        assigned_to=assigned_to,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_orders_filter_by_created_by(client: TestClient, db_session: Session) -> None:
    """created_by filter returns only orders created by that user."""
    user_a = _make_user(db_session, username="creator_a")
    user_b = _make_user(db_session, username="creator_b")
    token = _login(client, "creator_a")

    order_a = _make_order(db_session, created_by=user_a.id, customer_name="Corp A")
    _make_order(db_session, created_by=user_b.id, customer_name="Corp B")

    res = client.get(f"/api/v1/orders?created_by={user_a.id}", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["total"] >= 1
    ids = {item["id"] for item in body["items"]}
    assert str(order_a.id) in ids
    assert all(item["created_by"] == str(user_a.id) for item in body["items"])


def test_orders_filter_by_assigned_to_single(client: TestClient, db_session: Session) -> None:
    """assigned_to with a single UUID returns only orders assigned to that user."""
    creator = _make_user(db_session, username="creator_c")
    assignee = _make_user(db_session, username="assignee_a")
    token = _login(client, "creator_c")

    assigned = _make_order(db_session, created_by=creator.id, assigned_to=assignee.id)
    _make_order(db_session, created_by=creator.id)  # unassigned

    res = client.get(f"/api/v1/orders?assigned_to={assignee.id}", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["total"] >= 1
    ids = {item["id"] for item in body["items"]}
    assert str(assigned.id) in ids
    assert all(item["assigned_to"] == str(assignee.id) for item in body["items"])


def test_orders_filter_by_assigned_to_multiple(client: TestClient, db_session: Session) -> None:
    """assigned_to with multiple UUIDs returns union of orders assigned to any of them."""
    creator = _make_user(db_session, username="creator_d")
    assignee_1 = _make_user(db_session, username="assignee_b")
    assignee_2 = _make_user(db_session, username="assignee_c")
    token = _login(client, "creator_d")

    order_1 = _make_order(db_session, created_by=creator.id, assigned_to=assignee_1.id)
    order_2 = _make_order(db_session, created_by=creator.id, assigned_to=assignee_2.id)
    _make_order(db_session, created_by=creator.id)  # unassigned

    res = client.get(
        f"/api/v1/orders?assigned_to={assignee_1.id}&assigned_to={assignee_2.id}",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    ids = {item["id"] for item in body["items"]}
    assert str(order_1.id) in ids
    assert str(order_2.id) in ids


def test_orders_filter_created_by_and_assigned_to_combined(
    client: TestClient, db_session: Session
) -> None:
    """created_by and assigned_to filters are combined with AND logic."""
    creator = _make_user(db_session, username="creator_e")
    other_creator = _make_user(db_session, username="other_creator_a")
    assignee = _make_user(db_session, username="assignee_d")
    token = _login(client, "creator_e")

    target = _make_order(
        db_session, created_by=creator.id, assigned_to=assignee.id, customer_name="Target"
    )
    # Same creator, different assignee — should NOT appear
    _make_order(db_session, created_by=creator.id, customer_name="No Assignee")
    # Same assignee, different creator — should NOT appear
    _make_order(
        db_session,
        created_by=other_creator.id,
        assigned_to=assignee.id,
        customer_name="Other Creator",
    )

    res = client.get(
        f"/api/v1/orders?created_by={creator.id}&assigned_to={assignee.id}",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    ids = {item["id"] for item in body["items"]}
    assert str(target.id) in ids
    assert body["total"] >= 1
    assert all(
        item["created_by"] == str(creator.id) and item["assigned_to"] == str(assignee.id)
        for item in body["items"]
    )


def test_orders_filter_created_by_no_match_returns_empty(
    client: TestClient, db_session: Session
) -> None:
    """created_by with a non-existent UUID returns empty list with HTTP 200."""
    creator = _make_user(db_session, username="creator_f")
    token = _login(client, "creator_f")
    _make_order(db_session, created_by=creator.id)

    non_existent = uuid.uuid4()
    res = client.get(f"/api/v1/orders?created_by={non_existent}", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_orders_existing_filters_still_work(client: TestClient, db_session: Session) -> None:
    """Existing status and search filters continue to work after the new filters are added."""
    creator = _make_user(db_session, username="creator_g")
    token = _login(client, "creator_g")

    _make_order(
        db_session,
        created_by=creator.id,
        customer_name="Acme Corp",
        status=OrderStatus.pending,
    )
    _make_order(
        db_session,
        created_by=creator.id,
        customer_name="Beta Inc",
        status=OrderStatus.pending,
    )

    # Filter by status
    res_status = client.get("/api/v1/orders?status=pending", headers=_auth(token))
    assert res_status.status_code == 200
    assert res_status.json()["total"] >= 2

    # Filter by search (customer_name)
    res_search = client.get("/api/v1/orders?search=Acme", headers=_auth(token))
    assert res_search.status_code == 200
    assert res_search.json()["total"] >= 1
    assert all("Acme" in item["customer_name"] for item in res_search.json()["items"])
