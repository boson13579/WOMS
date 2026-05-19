"""Notification REST endpoints.

Route registration order matters: /read-all must precede /{notification_id}/read
so FastAPI does not interpret the literal string "read-all" as a UUID.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_roles
from app.models.user import User, UserRole
from app.schemas.notification import NotificationListResponse, NotificationResponse
from app.services import notification as notification_service

router = APIRouter()

_AUTH_ROLES = require_roles(
    UserRole.viewer,
    UserRole.order_manager,
    UserRole.scheduler,
    UserRole.root,
)


@router.get("", response_model=NotificationListResponse)
def list_notifications(
    all: bool = Query(default=False, alias="all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(_AUTH_ROLES),
) -> NotificationListResponse:
    """Return notifications for the authenticated user.

    By default only unread notifications are returned.
    Pass ``?all=true`` to include already-read notifications.
    """
    return notification_service.list_notifications(db, current_user.id, all_notifications=all)


@router.patch("/read-all", response_model=dict)
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(_AUTH_ROLES),
) -> dict[str, int]:
    """Mark all unread notifications as read for the authenticated user."""
    count = notification_service.mark_all_read(db, current_user.id)
    return {"updated": count}


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
def mark_notification_read(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(_AUTH_ROLES),
) -> NotificationResponse:
    """Mark a single notification as read.

    Returns 403 if the notification belongs to a different user.
    """
    result = notification_service.mark_read(db, notification_id, current_user.id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Notification not found or does not belong to you.",
        )
    return result
