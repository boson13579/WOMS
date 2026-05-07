"""Order CRUD API tests.

Covers: Create / Read / Update / Delete / Batch-Update / Audit-Log endpoints.

Run `pytest tests/api/test_orders.py -v` to execute.
"""

from __future__ import annotations

import uuid
from datetime import date

import bcrypt
from app.models.audit_log import AuditLog
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DELIVERY = "2026-08-01"


def _make_user(
    db: Session,
    *,
    username: str,
    password: str = "password123",
    role: UserRole = UserRole.viewer,
) -> User:
    user = User(
        username=username,
        password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client: TestClient, username: str, password: str = "password123") -> str:
    res = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200
    return res.json()["access_token"]


def _make_order(
    db: Session,
    *,
    created_by: uuid.UUID,
    customer_name: str = "Test Customer",
    wafer_quantity: int = 100,
    requested_delivery_date: date = date(2026, 8, 1),
    status: OrderStatus = OrderStatus.pending,
    order_number: str | None = None,
) -> Order:
    order = Order(
        order_number=order_number or f"ORD-TEST-{uuid.uuid4().hex[:6].upper()}",
        customer_name=customer_name,
        wafer_quantity=wafer_quantity,
        requested_delivery_date=requested_delivery_date,
        status=status,
        created_by=created_by,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_order_success(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_create", role=UserRole.scheduler)
    token = _login(client, "sched_create")

    res = client.post(
        "/api/v1/orders",
        json={
            "customer_name": "TSMC",
            "wafer_quantity": 200,
            "requested_delivery_date": _DELIVERY,
        },
        headers=_auth(token),
    )

    assert res.status_code == 201
    body = res.json()
    assert body["order_number"].startswith("ORD-")
    assert body["status"] == "pending"
    assert body["customer_name"] == "TSMC"
    assert body["wafer_quantity"] == 200
    assert body["created_by"] == str(user.id)


def test_create_order_invalid_quantity_too_low(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="sched_low", role=UserRole.scheduler)
    token = _login(client, "sched_low")

    res = client.post(
        "/api/v1/orders",
        json={"customer_name": "X", "wafer_quantity": 24, "requested_delivery_date": _DELIVERY},
        headers=_auth(token),
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == 422


def test_create_order_invalid_quantity_too_high(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="sched_high", role=UserRole.scheduler)
    token = _login(client, "sched_high")

    res = client.post(
        "/api/v1/orders",
        json={"customer_name": "X", "wafer_quantity": 2501, "requested_delivery_date": _DELIVERY},
        headers=_auth(token),
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == 422


def test_create_order_by_viewer_returns_403(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="viewer_create", role=UserRole.viewer)
    token = _login(client, "viewer_create")

    res = client.post(
        "/api/v1/orders",
        json={"customer_name": "X", "wafer_quantity": 100, "requested_delivery_date": _DELIVERY},
        headers=_auth(token),
    )

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_create_order_without_token_returns_401(client: TestClient) -> None:
    res = client.post(
        "/api/v1/orders",
        json={"customer_name": "X", "wafer_quantity": 100, "requested_delivery_date": _DELIVERY},
    )

    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def test_list_orders_returns_paginated_results(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="mgr_list", role=UserRole.order_manager)
    token = _login(client, "mgr_list")
    for _ in range(3):
        _make_order(db_session, created_by=user.id)

    res = client.get("/api/v1/orders", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert "items" in body
    assert "total" in body
    assert "page" in body
    assert "page_size" in body
    assert body["total"] >= 3
    assert body["page"] == 1
    assert body["page_size"] == 20


def test_list_orders_filter_by_status(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="mgr_filter_status", role=UserRole.order_manager)
    token = _login(client, "mgr_filter_status")
    _make_order(db_session, created_by=user.id, status=OrderStatus.pending)
    _make_order(db_session, created_by=user.id, status=OrderStatus.completed)

    res = client.get("/api/v1/orders?status=pending", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert all(item["status"] == "pending" for item in body["items"])


def test_list_orders_filter_by_assigned_to(client: TestClient, db_session: Session) -> None:
    mgr = _make_user(db_session, username="mgr_filter_assign", role=UserRole.order_manager)
    other = _make_user(db_session, username="other_user_filter", role=UserRole.viewer)
    token = _login(client, "mgr_filter_assign")
    # One order assigned to `other`, one with no assignment
    assigned_order = _make_order(db_session, created_by=mgr.id)
    assigned_order.assigned_to = other.id
    db_session.commit()
    _make_order(db_session, created_by=mgr.id)  # unassigned

    res = client.get(f"/api/v1/orders?assigned_to={other.id}", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert all(item["assigned_to"] == str(other.id) for item in body["items"])


def test_get_order_by_id_success(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="mgr_get", role=UserRole.order_manager)
    token = _login(client, "mgr_get")
    order = _make_order(db_session, created_by=user.id, customer_name="Get Corp")

    res = client.get(f"/api/v1/orders/{order.id}", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["id"] == str(order.id)
    assert body["customer_name"] == "Get Corp"


def test_get_order_not_found_returns_404(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="mgr_notfound", role=UserRole.order_manager)
    token = _login(client, "mgr_notfound")

    res = client.get(f"/api/v1/orders/{uuid.uuid4()}", headers=_auth(token))

    assert res.status_code == 404
    assert res.json()["error"]["code"] == 404


def test_list_orders_by_order_manager_succeeds(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="mgr_role_check", role=UserRole.order_manager)
    token = _login(client, "mgr_role_check")

    res = client.get("/api/v1/orders", headers=_auth(token))

    assert res.status_code == 200


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_order_success(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_upd", role=UserRole.scheduler)
    token = _login(client, "sched_upd")
    order = _make_order(db_session, created_by=user.id, wafer_quantity=100)

    res = client.patch(
        f"/api/v1/orders/{order.id}",
        json={"wafer_quantity": 200, "version_id": order.version_id},
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["wafer_quantity"] == 200
    assert body["status"] == "pending"


def test_update_order_version_conflict_returns_409(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_conflict", role=UserRole.scheduler)
    token = _login(client, "sched_conflict")
    order = _make_order(db_session, created_by=user.id)
    old_version_id = order.version_id

    # First update succeeds (bumps version_id)
    client.patch(
        f"/api/v1/orders/{order.id}",
        json={"wafer_quantity": 150, "version_id": old_version_id},
        headers=_auth(token),
    )

    # Second update with the stale version_id → 409
    res = client.patch(
        f"/api/v1/orders/{order.id}",
        json={"wafer_quantity": 200, "version_id": old_version_id},
        headers=_auth(token),
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_update_order_in_production_returns_422(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_prod", role=UserRole.scheduler)
    token = _login(client, "sched_prod")
    order = _make_order(db_session, created_by=user.id, status=OrderStatus.in_production)

    res = client.patch(
        f"/api/v1/orders/{order.id}",
        json={"wafer_quantity": 200, "version_id": order.version_id},
        headers=_auth(token),
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == 422


def test_update_order_cancelled_returns_422(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_cancel_upd", role=UserRole.scheduler)
    token = _login(client, "sched_cancel_upd")
    order = _make_order(db_session, created_by=user.id, status=OrderStatus.cancelled)

    res = client.patch(
        f"/api/v1/orders/{order.id}",
        json={"wafer_quantity": 200, "version_id": order.version_id},
        headers=_auth(token),
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == 422


def test_update_order_not_found_returns_404(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="sched_upd_404", role=UserRole.scheduler)
    token = _login(client, "sched_upd_404")

    res = client.patch(
        f"/api/v1/orders/{uuid.uuid4()}",
        json={"wafer_quantity": 100, "version_id": 1},
        headers=_auth(token),
    )

    assert res.status_code == 404
    assert res.json()["error"]["code"] == 404


def test_update_order_partial_fields(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_partial", role=UserRole.scheduler)
    token = _login(client, "sched_partial")
    order = _make_order(
        db_session,
        created_by=user.id,
        wafer_quantity=100,
        requested_delivery_date=date(2026, 8, 1),
    )

    res = client.patch(
        f"/api/v1/orders/{order.id}",
        json={"notes": "urgent shipment", "version_id": order.version_id},
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["notes"] == "urgent shipment"
    assert body["wafer_quantity"] == 100
    assert body["requested_delivery_date"] == "2026-08-01"
    assert body["status"] == "pending"


def test_update_order_only_delivery_date(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_delivery", role=UserRole.scheduler)
    token = _login(client, "sched_delivery")
    order = _make_order(
        db_session,
        created_by=user.id,
        wafer_quantity=100,
        requested_delivery_date=date(2026, 8, 1),
    )

    res = client.patch(
        f"/api/v1/orders/{order.id}",
        json={"requested_delivery_date": "2026-09-30", "version_id": order.version_id},
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["requested_delivery_date"] == "2026-09-30"
    assert body["wafer_quantity"] == 100
    assert body["notes"] is None
    assert body["status"] == "pending"


# ---------------------------------------------------------------------------
# Delete (soft)
# ---------------------------------------------------------------------------


def test_delete_order_sets_cancelled_and_soft_deleted(
    client: TestClient, db_session: Session
) -> None:
    user = _make_user(db_session, username="sched_del", role=UserRole.scheduler)
    token = _login(client, "sched_del")
    order = _make_order(db_session, created_by=user.id)

    res = client.delete(f"/api/v1/orders/{order.id}", headers=_auth(token))

    assert res.status_code == 204
    assert res.content == b""

    db_session.refresh(order)
    assert order.is_deleted is True
    assert order.status == OrderStatus.cancelled

    # Soft-deleted order is invisible via the normal GET endpoint
    get_res = client.get(f"/api/v1/orders/{order.id}", headers=_auth(token))
    assert get_res.status_code == 404


def test_delete_nonexistent_order_returns_404(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="sched_del_404", role=UserRole.scheduler)
    token = _login(client, "sched_del_404")

    res = client.delete(f"/api/v1/orders/{uuid.uuid4()}", headers=_auth(token))

    assert res.status_code == 404
    assert res.json()["error"]["code"] == 404


# ---------------------------------------------------------------------------
# Batch update
# ---------------------------------------------------------------------------


def test_batch_update_success(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_batch_ok", role=UserRole.scheduler)
    token = _login(client, "sched_batch_ok")
    o1 = _make_order(db_session, created_by=user.id, status=OrderStatus.pending)
    o2 = _make_order(db_session, created_by=user.id, status=OrderStatus.scheduled)

    new_date = "2026-09-15"
    res = client.patch(
        "/api/v1/orders/batch-update",
        json={"order_ids": [str(o1.id), str(o2.id)], "requested_delivery_date": new_date},
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["updated_count"] == 2
    assert body["skipped_count"] == 0
    assert body["skipped_ids"] == []


def test_batch_update_skips_immutable_orders(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_batch_skip", role=UserRole.scheduler)
    token = _login(client, "sched_batch_skip")
    o_pending = _make_order(db_session, created_by=user.id, status=OrderStatus.pending)
    o_locked = _make_order(db_session, created_by=user.id, status=OrderStatus.in_production)

    res = client.patch(
        "/api/v1/orders/batch-update",
        json={
            "order_ids": [str(o_pending.id), str(o_locked.id)],
            "requested_delivery_date": "2026-09-20",
        },
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["updated_count"] == 1
    assert body["skipped_count"] == 1
    assert str(o_locked.id) in body["skipped_ids"]


def test_batch_update_all_skipped_returns_200(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_batch_all_skip", role=UserRole.scheduler)
    token = _login(client, "sched_batch_all_skip")
    o = _make_order(db_session, created_by=user.id, status=OrderStatus.completed)

    res = client.patch(
        "/api/v1/orders/batch-update",
        json={"order_ids": [str(o.id)], "requested_delivery_date": "2026-09-20"},
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["updated_count"] == 0
    assert body["skipped_count"] == 1


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_log_recorded_on_create(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="sched_audit_create", role=UserRole.scheduler)
    token = _login(client, "sched_audit_create")

    res = client.post(
        "/api/v1/orders",
        json={
            "customer_name": "AuditCo",
            "wafer_quantity": 50,
            "requested_delivery_date": _DELIVERY,
        },
        headers=_auth(token),
    )

    assert res.status_code == 201
    order_id = uuid.UUID(res.json()["id"])

    logs = db_session.scalars(select(AuditLog).where(AuditLog.resource_id == order_id)).all()
    assert len(logs) >= 1
    assert any(log.action == "order.created" for log in logs)


def test_audit_log_recorded_on_update(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_audit_upd", role=UserRole.scheduler)
    token = _login(client, "sched_audit_upd")
    order = _make_order(db_session, created_by=user.id)

    client.patch(
        f"/api/v1/orders/{order.id}",
        json={"wafer_quantity": 300, "version_id": order.version_id},
        headers=_auth(token),
    )

    logs = db_session.scalars(
        select(AuditLog).where(AuditLog.resource_id == order.id, AuditLog.action == "order.updated")
    ).all()
    assert len(logs) >= 1


def test_audit_log_recorded_on_cancel(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="sched_audit_cancel", role=UserRole.scheduler)
    token = _login(client, "sched_audit_cancel")
    order = _make_order(db_session, created_by=user.id)

    client.delete(f"/api/v1/orders/{order.id}", headers=_auth(token))

    logs = db_session.scalars(
        select(AuditLog).where(
            AuditLog.resource_id == order.id, AuditLog.action == "order.cancelled"
        )
    ).all()
    assert len(logs) >= 1


def test_get_audit_log_by_order_id(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="mgr_audit_get", role=UserRole.order_manager)
    _make_user(db_session, username="sched_audit_get", role=UserRole.scheduler)
    mgr_token = _login(client, "mgr_audit_get")
    sched_token = _login(client, "sched_audit_get")

    # Create order via API so audit log is written
    res = client.post(
        "/api/v1/orders",
        json={
            "customer_name": "LogCo",
            "wafer_quantity": 75,
            "requested_delivery_date": _DELIVERY,
        },
        headers=_auth(sched_token),
    )
    assert res.status_code == 201
    order_id = res.json()["id"]

    res = client.get(f"/api/v1/orders/{order_id}/audit-log", headers=_auth(mgr_token))

    assert res.status_code == 200
    logs = res.json()
    assert isinstance(logs, list)
    assert len(logs) >= 1
    assert logs[0]["action"] == "order.created"


def test_get_audit_log_not_found_returns_404(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="mgr_audit_404", role=UserRole.order_manager)
    token = _login(client, "mgr_audit_404")

    res = client.get(f"/api/v1/orders/{uuid.uuid4()}/audit-log", headers=_auth(token))

    assert res.status_code == 404
    assert res.json()["error"]["code"] == 404


def test_get_audit_log_after_cancel_still_returns_logs(
    client: TestClient, db_session: Session
) -> None:
    _make_user(db_session, username="sched_audit_cancel2", role=UserRole.scheduler)
    _make_user(db_session, username="mgr_audit_cancel2", role=UserRole.order_manager)
    sched_token = _login(client, "sched_audit_cancel2")
    mgr_token = _login(client, "mgr_audit_cancel2")

    # Create order via API (writes order.created audit log)
    res = client.post(
        "/api/v1/orders",
        json={
            "customer_name": "CancelCo",
            "wafer_quantity": 50,
            "requested_delivery_date": _DELIVERY,
        },
        headers=_auth(sched_token),
    )
    assert res.status_code == 201
    order_id = res.json()["id"]

    # Cancel the order (soft-delete, writes order.cancelled audit log)
    res = client.delete(f"/api/v1/orders/{order_id}", headers=_auth(sched_token))
    assert res.status_code == 204

    # Audit log must still be queryable even though the order is soft-deleted
    res = client.get(f"/api/v1/orders/{order_id}/audit-log", headers=_auth(mgr_token))

    assert res.status_code == 200
    logs = res.json()
    assert isinstance(logs, list)
    assert len(logs) >= 2
    actions = [log["action"] for log in logs]
    assert "order.created" in actions
    assert "order.cancelled" in actions


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_list_orders_search_by_order_number(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="mgr_search_num", role=UserRole.order_manager)
    token = _login(client, "mgr_search_num")
    _make_order(db_session, created_by=user.id, order_number="ORD-MATCH-0001")
    _make_order(db_session, created_by=user.id, order_number="ORD-OTHER-0002")

    res = client.get("/api/v1/orders?search=MATCH", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    order_numbers = [o["order_number"] for o in body["items"]]
    assert "ORD-MATCH-0001" in order_numbers
    assert "ORD-OTHER-0002" not in order_numbers


def test_list_orders_search_by_customer_name(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="mgr_search_cust", role=UserRole.order_manager)
    token = _login(client, "mgr_search_cust")
    _make_order(db_session, created_by=user.id, customer_name="Searchable Corp")
    _make_order(db_session, created_by=user.id, customer_name="Other Company")

    res = client.get("/api/v1/orders?search=Searchable", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    customer_names = [o["customer_name"] for o in body["items"]]
    assert "Searchable Corp" in customer_names
    assert "Other Company" not in customer_names
