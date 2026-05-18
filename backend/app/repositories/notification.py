"""Pure CRUD operations for the Notification entity."""

from __future__ import annotations

import uuid
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.notification import Notification

__all__ = [
    "create",
    "list_by_user",
    "mark_all_read",
    "mark_read",
]


def create(
    db: Session,
    *,
    user_id: uuid.UUID,
    order_id: uuid.UUID | None,
    type: str,
    message: str,
) -> Notification:
    """Persist a new notification row and return the flushed instance."""
    notif = Notification(
        user_id=user_id,
        order_id=order_id,
        type=type,
        message=message,
    )
    db.add(notif)
    db.flush()
    return notif


def list_by_user(
    db: Session,
    user_id: uuid.UUID,
    *,
    unread_only: bool = True,
) -> list[Notification]:
    """Return notifications for *user_id*, newest first.

    Pass *unread_only=False* to include already-read notifications.
    """
    stmt = select(Notification).where(
        Notification.user_id == user_id,
        Notification.is_deleted.is_(False),
    )
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    stmt = stmt.order_by(Notification.created_at.desc())
    return list(db.scalars(stmt).all())


def mark_read(
    db: Session,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Notification | None:
    """Set *is_read=True* on the given notification.

    Returns the updated row, or None if it does not exist or belongs to a
    different user.
    """
    notif = db.scalars(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.is_deleted.is_(False),
        )
    ).first()
    if notif is None or notif.user_id != user_id:
        return None
    notif.is_read = True
    db.flush()
    return notif


def mark_all_read(db: Session, user_id: uuid.UUID) -> int:
    """Mark every unread notification for *user_id* as read.

    Returns the number of rows updated.
    """
    cursor = cast(
        CursorResult[object],
        db.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
                Notification.is_deleted.is_(False),
            )
            .values(is_read=True)
            .execution_options(synchronize_session="fetch")
        ),
    )
    return cursor.rowcount
