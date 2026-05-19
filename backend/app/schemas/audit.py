"""Pydantic DTOs for the cross-resource audit-log domain.

Originally lived under ``schemas/order.py`` because the only consumer was the
``GET /orders/{id}/audit-log`` endpoint. PR B3 introduces a second consumer
(``GET /users/{id}/audit``) and ``audit_logs`` rows are written by the auth,
user, order, scheduler, and worker layers alike. Keeping the DTO under
``schemas/order.py`` was a historical accident — moving it here makes the
cross-resource nature explicit. ``schemas/order.py`` keeps a re-export shim so
existing imports (``from app.schemas.order import AuditLogResponse``) keep
working.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = [
    "AuditLogResponse",
    "UserAuditLogListResponse",
]


class AuditLogResponse(BaseModel):
    """Single audit-log entry for any resource (order, user, worker, ...).

    ``user_id`` is nullable because system-driven actions (e.g. the scheduler
    applying a computed result via ``order.scheduled``) have no human actor.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: str
    user_id: uuid.UUID | None
    resource_id: uuid.UUID
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    created_at: datetime


class UserAuditLogListResponse(BaseModel):
    """Paginated list of audit-log entries for a single user.

    Shape mirrors ``OrderListResponse`` (``items`` / ``total`` / ``page`` /
    ``page_size``) so the frontend can reuse its existing paginator widget.
    Verifier-mandated decision: pick ``page``/``page_size`` over
    ``limit``/``offset`` to match ``GET /orders`` and minimise FE consumer
    confusion. ``GET /orders/{id}/audit-log`` is unpaginated (bare list);
    this endpoint is paginated because an admin views may scroll through
    long histories.
    """

    items: list[AuditLogResponse]
    total: int
    page: int
    page_size: int
