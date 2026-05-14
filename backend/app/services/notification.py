"""Notification business logic — create, query, and mark-read operations."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.repositories import notification as notification_repo
from app.schemas.notification import NotificationListResponse, NotificationResponse
from app.services.websocket import notify_user

logger = structlog.get_logger(__name__)

__all__ = [
    "create_notification",
    "list_notifications",
    "mark_all_read",
    "mark_read",
]


def create_notification(
    db: Session,
    *,
    user_id: uuid.UUID,
    order_id: uuid.UUID | None,
    type: str,
    message: str,
) -> NotificationResponse:
    """Persist a notification and push it to the user via WebSocket.

    The WebSocket broadcast is best-effort: if *notify_user* raises, the
    exception is logged and swallowed so the DB write is never rolled back.
    """
    notif = notification_repo.create(
        db,
        user_id=user_id,
        order_id=order_id,
        type=type,
        message=message,
    )
    db.commit()
    db.refresh(notif)
    resp = NotificationResponse.model_validate(notif)

    payload: dict[str, Any] = {
        "type": "notification.created",
        "data": resp.model_dump(mode="json"),
    }
    try:
        notify_user(user_id=user_id, message=payload)
    except Exception:
        logger.warning(
            "notification.broadcast_failed",
            user_id=str(user_id),
            notification_id=str(notif.id),
        )

    return resp


def list_notifications(
    db: Session,
    user_id: uuid.UUID,
    *,
    all_notifications: bool = False,
) -> NotificationListResponse:
    """Return notifications for *user_id*.

    By default only unread notifications are returned.
    Pass *all_notifications=True* to include read ones.
    """
    items = notification_repo.list_by_user(db, user_id, unread_only=not all_notifications)
    responses = [NotificationResponse.model_validate(n) for n in items]
    return NotificationListResponse(items=responses, total=len(responses))


def mark_read(
    db: Session,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
) -> NotificationResponse | None:
    """Mark a single notification as read.

    Returns the updated notification, or None if not found / wrong owner.
    """
    notif = notification_repo.mark_read(db, notification_id, user_id)
    if notif is None:
        return None
    db.commit()
    db.refresh(notif)
    return NotificationResponse.model_validate(notif)


def mark_all_read(db: Session, user_id: uuid.UUID) -> int:
    """Mark all unread notifications for *user_id* as read.

    Returns the number of rows updated.
    """
    count = notification_repo.mark_all_read(db, user_id)
    db.commit()
    return count
