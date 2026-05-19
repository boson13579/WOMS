"""Pydantic DTOs for the cross-resource audit-log domain.

Originally lived under ``schemas/order.py`` because the only consumer was the
``GET /orders/{id}/audit-log`` endpoint. PR B3 introduced a second consumer
(``GET /users/{id}/audit``) and ``audit_logs`` rows are written by the auth,
user, order, scheduler, and worker layers alike. Keeping the DTO under
``schemas/order.py`` was a historical accident — moving it here makes the
cross-resource nature explicit. ``schemas/order.py`` keeps a re-export shim so
existing imports (``from app.schemas.order import AuditLogResponse``) keep
working.

PR A3 (Observability) widens the consumer set again with the global
``GET /audit/events`` admin feed. The list-wrapper DTO is therefore renamed
from ``UserAuditLogListResponse`` → ``AuditLogListResponse`` (same shape) to
reflect that it now models any paginated audit-log slice, not just one
user's history. The old name remains as a re-export alias so the per-user
endpoint imports (and any downstream consumers) keep working without churn.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = [
    "AuditLogListResponse",
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


class AuditLogListResponse(BaseModel):
    """Paginated list of audit-log entries.

    Shape mirrors ``OrderListResponse`` (``items`` / ``total`` / ``page`` /
    ``page_size``) so the frontend can reuse its existing paginator widget.
    Used by both the per-user (``GET /users/{id}/audit``) and global
    (``GET /audit/events``) audit feeds — the only difference is the filter
    set applied at the repository layer.

    Verifier-mandated decision: pick ``page``/``page_size`` over
    ``limit``/``offset`` to match ``GET /orders`` and minimise FE consumer
    confusion. ``GET /orders/{id}/audit-log`` is unpaginated (bare list);
    these endpoints are paginated because an admin view may scroll through
    long histories.
    """

    items: list[AuditLogResponse]
    total: int
    page: int
    page_size: int


# Backwards-compatibility alias for PR B3 callers (per-user audit endpoint
# imports the old name). New code should prefer ``AuditLogListResponse``.
UserAuditLogListResponse = AuditLogListResponse
