"""Notification API and service tests — TDD [RED] phase.

All 8 tests must FAIL before implementation begins.

Run: pytest tests/api/test_notifications.py -v
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import patch

import bcrypt
from app.models.notification import Notification
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from app.services import notification as notification_service
from app.services import order as order_service
from app.services.scheduling import ScheduledResult
from app.workers.scheduling import _perform_compound_db_action
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    username: str,
    password: str = "pass123",
    role: UserRole = UserRole.order_manager,
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


def _login(client: TestClient, username: str, password: str = "pass123") -> str:
    res = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200
    return res.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_order(
    db: Session,
    *,
    created_by: uuid.UUID,
    order_number: str | None = None,
    status: OrderStatus = OrderStatus.pending,
) -> Order:
    order = Order(
        order_number=order_number or f"ORD-N-{uuid.uuid4().hex[:6].upper()}",
        customer_name="Notify Customer",
        wafer_quantity=100,
        requested_delivery_date=date(2026, 9, 1),
        status=status,
        created_by=created_by,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _make_notification(
    db: Session,
    *,
    user_id: uuid.UUID,
    order_id: uuid.UUID | None = None,
    type: str = "order_status_changed",
    message: str = "test message",
    is_read: bool = False,
) -> Notification:
    n = Notification(
        user_id=user_id,
        order_id=order_id,
        type=type,
        message=message,
        is_read=is_read,
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


# ---------------------------------------------------------------------------
# [RED] Test 1 — GET /notifications returns unread only by default
# ---------------------------------------------------------------------------


def test_get_notifications_returns_unread_by_default(
    client: TestClient, db_session: Session
) -> None:
    user = _make_user(db_session, username="notif_user1")
    token = _login(client, "notif_user1")
    order = _make_order(db_session, created_by=user.id)

    _make_notification(db_session, user_id=user.id, order_id=order.id, is_read=False)
    _make_notification(db_session, user_id=user.id, order_id=order.id, is_read=False)
    _make_notification(db_session, user_id=user.id, order_id=order.id, is_read=True)

    res = client.get("/api/v1/notifications", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert all(not item["is_read"] for item in body["items"])


# ---------------------------------------------------------------------------
# [RED] Test 2 — GET /notifications?all=true returns all notifications
# ---------------------------------------------------------------------------


def test_get_notifications_all_param(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="notif_user2")
    token = _login(client, "notif_user2")
    order = _make_order(db_session, created_by=user.id)

    _make_notification(db_session, user_id=user.id, order_id=order.id, is_read=False)
    _make_notification(db_session, user_id=user.id, order_id=order.id, is_read=False)
    _make_notification(db_session, user_id=user.id, order_id=order.id, is_read=True)

    res = client.get("/api/v1/notifications?all=true", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3


# ---------------------------------------------------------------------------
# [RED] Test 3 — PATCH /notifications/{id}/read marks as read
# ---------------------------------------------------------------------------


def test_mark_notification_read(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="notif_user3")
    token = _login(client, "notif_user3")
    notif = _make_notification(db_session, user_id=user.id, is_read=False)

    res = client.patch(
        f"/api/v1/notifications/{notif.id}/read",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["is_read"] is True
    assert body["id"] == str(notif.id)


# ---------------------------------------------------------------------------
# [RED] Test 4 — PATCH /{id}/read on another user's notification → 403
# ---------------------------------------------------------------------------


def test_mark_notification_read_other_user_forbidden(
    client: TestClient, db_session: Session
) -> None:
    owner = _make_user(db_session, username="notif_owner4")
    _make_user(db_session, username="notif_other4")
    other_token = _login(client, "notif_other4")

    notif = _make_notification(db_session, user_id=owner.id, is_read=False)

    res = client.patch(
        f"/api/v1/notifications/{notif.id}/read",
        headers=_auth(other_token),
    )

    assert res.status_code == 403


# ---------------------------------------------------------------------------
# [RED] Test 5 — PATCH /notifications/read-all marks all unread
# ---------------------------------------------------------------------------


def test_mark_all_read(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, username="notif_user5")
    token = _login(client, "notif_user5")
    order = _make_order(db_session, created_by=user.id)

    _make_notification(db_session, user_id=user.id, order_id=order.id, is_read=False)
    _make_notification(db_session, user_id=user.id, order_id=order.id, is_read=False)
    _make_notification(db_session, user_id=user.id, order_id=order.id, is_read=True)

    res = client.patch("/api/v1/notifications/read-all", headers=_auth(token))

    assert res.status_code == 200

    # Verify all are now read in DB
    db_session.expire_all()
    remaining = db_session.scalars(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.is_read.is_(False),
        )
    ).all()
    assert len(remaining) == 0


# ---------------------------------------------------------------------------
# [RED] Test 6 — order lock triggers Notification record in DB
# ---------------------------------------------------------------------------


def test_order_lock_creates_notification(client: TestClient, db_session: Session) -> None:
    """Locking an order (is_processing_locked=True) must create an order_locked notification."""
    user = _make_user(db_session, username="notif_user6", role=UserRole.scheduler)
    token = _login(client, "notif_user6")
    order = _make_order(db_session, created_by=user.id)

    # PATCH with scheduling change triggers is_processing_locked=True and notification hook
    res = client.patch(
        f"/api/v1/orders/{order.id}",
        json={
            "wafer_quantity": 200,
            "requested_delivery_date": "2026-10-01",
            "version_id": order.version_id,
        },
        headers=_auth(token),
    )

    # Regardless of scheduling outcome, the producer-side lock + notification should fire
    assert res.status_code in (200, 202)

    db_session.expire_all()
    notifs = db_session.scalars(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.order_id == order.id,
            Notification.type == "order_locked",
        )
    ).all()
    assert len(notifs) == 1


# ---------------------------------------------------------------------------
# [RED] Test 7 — notify_user is called when notification is created
# ---------------------------------------------------------------------------


def test_notification_broadcast_called_on_create(db_session: Session) -> None:
    """notification_service.create_notification must call notify_user after DB write."""
    user = _make_user(db_session, username="notif_user7")
    order = _make_order(db_session, created_by=user.id)

    with patch("app.services.notification.notify_user") as mock_notify:
        notification_service.create_notification(
            db_session,
            user_id=user.id,
            order_id=order.id,
            type="order_locked",
            message=f"訂單 {order.order_number} 已被鎖定處理中",
        )

    mock_notify.assert_called_once()
    call_kwargs = mock_notify.call_args.kwargs
    assert call_kwargs["user_id"] == user.id
    assert call_kwargs["message"]["type"] == "notification.created"


# ---------------------------------------------------------------------------
# [RED] Test 8 — broadcast failure does NOT rollback DB write
# ---------------------------------------------------------------------------


def test_broadcast_failure_does_not_rollback(db_session: Session) -> None:
    """If notify_user raises, the notification must still be committed to DB."""
    user = _make_user(db_session, username="notif_user8")
    order = _make_order(db_session, created_by=user.id)

    with patch(
        "app.services.notification.notify_user",
        side_effect=RuntimeError("ws connection lost"),
    ):
        result = notification_service.create_notification(
            db_session,
            user_id=user.id,
            order_id=order.id,
            type="order_locked",
            message=f"訂單 {order.order_number} 已被鎖定處理中",
        )

    # Notification must be persisted despite WS failure
    assert result is not None
    db_session.expire_all()
    notif = db_session.get(Notification, result.id)
    assert notif is not None
    assert notif.user_id == user.id


# ---------------------------------------------------------------------------
# Test 9 — apply_schedule triggers order_status_changed notification
# ---------------------------------------------------------------------------


def test_apply_schedule_creates_status_notification(db_session: Session) -> None:
    """order_service.apply_schedule must create an order_status_changed notification."""
    user = _make_user(db_session, username="notif_schedule_user")
    order = _make_order(db_session, created_by=user.id)

    results = [
        ScheduledResult(
            order_id=order.id,
            scheduled_date=date(2026, 9, 1),
            quantity=100,
        )
    ]
    with patch("app.services.notification.notify_user"):
        order_service.apply_schedule(db_session, results)

    db_session.expire_all()
    notifs = db_session.scalars(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.order_id == order.id,
            Notification.type == "order_status_changed",
        )
    ).all()
    assert len(notifs) == 1


# ---------------------------------------------------------------------------
# Test 10 — _perform_compound_db_action (delete accepted) triggers order_cancelled
# ---------------------------------------------------------------------------


def test_order_cancelled_notification(db_session: Session) -> None:
    """Worker cancel path (kind=delete, accepted=True) must create order_cancelled notification."""
    user = _make_user(db_session, username="notif_cancel_user")
    order = _make_order(db_session, created_by=user.id)

    compound = {
        "ops": [{"order_id": str(order.id)}],
        "db_action": {
            "kind": "delete",
            "actor_id": str(user.id),
            "old_wafer_quantity": order.wafer_quantity,
            "old_requested_delivery_date": order.requested_delivery_date.isoformat(),
        },
    }

    # Redirect _perform_compound_db_action's SessionLocal to the test session so
    # the worker can see records committed via savepoints, and prevent db.close()
    # from tearing down the test session.
    with patch("app.workers.scheduling.SessionLocal", return_value=db_session):
        with patch.object(db_session, "close"):
            with patch("app.services.notification.notify_user"):
                _perform_compound_db_action(compound, accepted=True)

    db_session.expire_all()
    notifs = db_session.scalars(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.order_id == order.id,
            Notification.type == "order_cancelled",
        )
    ).all()
    assert len(notifs) == 1
