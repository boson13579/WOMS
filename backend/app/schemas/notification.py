"""Pydantic DTOs for the notification domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

__all__ = [
    "NotificationListResponse",
    "NotificationResponse",
]


class NotificationResponse(BaseModel):
    """Single notification record returned to the client."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    order_id: uuid.UUID | None
    type: str
    message: str
    is_read: bool
    created_at: datetime


class NotificationListResponse(BaseModel):
    """List of notifications for a user."""

    items: list[NotificationResponse]
    total: int
