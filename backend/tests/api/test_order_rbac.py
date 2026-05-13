"""RBAC tests for order_manager ownership checks.

order_manager can CRUD their own orders but not others'.
Viewer cannot write orders but can PATCH /users/me.

Lock / soft-pin tests are skipped until feat/order-lock-mechanism merges.
"""

from __future__ import annotations

import uuid
from datetime import date

import bcrypt
import pytest
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

_DELIVERY = "2026-08-01"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    username: str,
    password: str = "password123",
    role: UserRole = UserRole.viewer,
) -> User:
    user = User(
        username=username,
        email=f"{username}@test.internal",
        password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_order(
    db: Session,
    *,
    created_by: uuid.UUID,
    wafer_quantity: int = 100,
    order_number: str | None = None,
) -> Order:
    order = Order(
        order_number=order_number or f"ORD-RBAC-{uuid.uuid4().hex[:6].upper()}",
        customer_name="RBAC Test Customer",
        wafer_quantity=wafer_quantity,
        requested_delivery_date=date(2026, 8, 1),
        status=OrderStatus.pending,
        created_by=created_by,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _login(client: TestClient, username: str, password: str = "password123") -> str:
    res = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200
    return res.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Create — POST /orders
# ---------------------------------------------------------------------------


def test_order_manager_can_create_order(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="mgr_create", role=UserRole.order_manager)
    token = _login(client, "mgr_create")

    res = client.post(
        "/api/v1/orders",
        json={
            "customer_name": "TSMC",
            "wafer_quantity": 100,
            "requested_delivery_date": _DELIVERY,
        },
        headers=_auth(token),
    )

    assert res.status_code == 201


def test_viewer_cannot_create_order_returns_403(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="viewer_create", role=UserRole.viewer)
    token = _login(client, "viewer_create")

    res = client.post(
        "/api/v1/orders",
        json={
            "customer_name": "TSMC",
            "wafer_quantity": 100,
            "requested_delivery_date": _DELIVERY,
        },
        headers=_auth(token),
    )

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


# ---------------------------------------------------------------------------
# Update — PATCH /orders/{order_id}
# ---------------------------------------------------------------------------


def test_order_manager_can_update_own_order(client: TestClient, db_session: Session) -> None:
    mgr = _make_user(db_session, username="mgr_upd", role=UserRole.order_manager)
    order = _make_order(db_session, created_by=mgr.id)
    token = _login(client, "mgr_upd")

    res = client.patch(
        f"/api/v1/orders/{order.id}",
        json={"notes": "Updated note.", "version_id": order.version_id},
        headers=_auth(token),
    )

    assert res.status_code == 200
    assert res.json()["notes"] == "Updated note."


def test_order_manager_cannot_update_others_order_returns_403(
    client: TestClient, db_session: Session
) -> None:
    owner = _make_user(db_session, username="owner_upd", role=UserRole.scheduler)
    _make_user(db_session, username="mgr_upd2", role=UserRole.order_manager)
    order = _make_order(db_session, created_by=owner.id)
    token = _login(client, "mgr_upd2")

    res = client.patch(
        f"/api/v1/orders/{order.id}",
        json={"notes": "Stolen note", "version_id": order.version_id},
        headers=_auth(token),
    )

    assert res.status_code == 403
    assert "only modify orders you created" in res.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Delete — DELETE /orders/{order_id}
# ---------------------------------------------------------------------------


def test_order_manager_can_cancel_own_order(client: TestClient, db_session: Session) -> None:
    mgr = _make_user(db_session, username="mgr_del", role=UserRole.order_manager)
    order = _make_order(db_session, created_by=mgr.id)
    token = _login(client, "mgr_del")

    res = client.delete(f"/api/v1/orders/{order.id}", headers=_auth(token))

    assert res.status_code == 204


def test_order_manager_cannot_cancel_others_order_returns_403(
    client: TestClient, db_session: Session
) -> None:
    owner = _make_user(db_session, username="owner_del", role=UserRole.scheduler)
    _make_user(db_session, username="mgr_del2", role=UserRole.order_manager)
    order = _make_order(db_session, created_by=owner.id)
    token = _login(client, "mgr_del2")

    res = client.delete(f"/api/v1/orders/{order.id}", headers=_auth(token))

    assert res.status_code == 403
    assert "only modify orders you created" in res.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Batch update — PATCH /orders/batch-update
# ---------------------------------------------------------------------------


def test_order_manager_batch_update_skips_others_orders(
    client: TestClient, db_session: Session
) -> None:
    owner = _make_user(db_session, username="owner_batch", role=UserRole.scheduler)
    mgr = _make_user(db_session, username="mgr_batch", role=UserRole.order_manager)
    own_order = _make_order(db_session, created_by=mgr.id)
    others_order = _make_order(db_session, created_by=owner.id)
    token = _login(client, "mgr_batch")

    res = client.patch(
        "/api/v1/orders/batch-update",
        json={
            "order_ids": [str(own_order.id), str(others_order.id)],
            "requested_delivery_date": _DELIVERY,
        },
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["updated_count"] == 1
    assert body["skipped_count"] == 1
    assert str(others_order.id) in [str(i) for i in body["skipped_ids"]]


# ---------------------------------------------------------------------------
# Self-update — PATCH /users/me (any authenticated role)
# ---------------------------------------------------------------------------


def test_viewer_can_update_self(client: TestClient, db_session: Session) -> None:
    viewer = _make_user(db_session, username="viewer_self", role=UserRole.viewer)
    token = _login(client, "viewer_self")

    res = client.patch(
        "/api/v1/users/me",
        json={"username": "viewer_self_updated", "version_id": viewer.version_id},
        headers=_auth(token),
    )

    assert res.status_code == 200
    assert res.json()["username"] == "viewer_self_updated"


# ---------------------------------------------------------------------------
# Lock / soft-pin (skipped — depend on feat/order-lock-mechanism)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Requires feat/order-lock-mechanism: Order.is_locked not on main")
def test_order_manager_can_lock_own_order(client: TestClient, db_session: Session) -> None:
    mgr = _make_user(db_session, username="mgr_lock", role=UserRole.order_manager)
    order = _make_order(db_session, created_by=mgr.id)
    token = _login(client, "mgr_lock")

    res = client.post(f"/api/v1/orders/{order.id}/lock", headers=_auth(token))

    assert res.status_code == 200


@pytest.mark.skip(reason="Requires feat/order-lock-mechanism: Order.is_locked not on main")
def test_order_manager_cannot_lock_others_order_returns_403(
    client: TestClient, db_session: Session
) -> None:
    owner = _make_user(db_session, username="owner_lock", role=UserRole.scheduler)
    _make_user(db_session, username="mgr_lock2", role=UserRole.order_manager)
    order = _make_order(db_session, created_by=owner.id)
    token = _login(client, "mgr_lock2")

    res = client.post(f"/api/v1/orders/{order.id}/lock", headers=_auth(token))

    assert res.status_code == 403
    assert "only modify orders you created" in res.json()["error"]["message"]


@pytest.mark.skip(reason="Requires feat/order-lock-mechanism: Order.soft_pin_date not on main")
def test_order_manager_can_set_soft_pin_own_order(client: TestClient, db_session: Session) -> None:
    mgr = _make_user(db_session, username="mgr_pin", role=UserRole.order_manager)
    order = _make_order(db_session, created_by=mgr.id)
    token = _login(client, "mgr_pin")

    res = client.patch(
        f"/api/v1/orders/{order.id}/soft-pin",
        json={"preferred_date": "2026-07-20"},
        headers=_auth(token),
    )

    assert res.status_code == 200


@pytest.mark.skip(reason="Requires feat/order-lock-mechanism: Order.soft_pin_date not on main")
def test_order_manager_cannot_set_soft_pin_others_order_returns_403(
    client: TestClient, db_session: Session
) -> None:
    owner = _make_user(db_session, username="owner_pin", role=UserRole.scheduler)
    _make_user(db_session, username="mgr_pin2", role=UserRole.order_manager)
    order = _make_order(db_session, created_by=owner.id)
    token = _login(client, "mgr_pin2")

    res = client.patch(
        f"/api/v1/orders/{order.id}/soft-pin",
        json={"preferred_date": "2026-07-20"},
        headers=_auth(token),
    )

    assert res.status_code == 403
    assert "only modify orders you created" in res.json()["error"]["message"]
