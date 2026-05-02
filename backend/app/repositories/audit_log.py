"""Pure CRUD operations for the AuditLog entity."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog

__all__ = [
    "create",
    "get_by_resource_id",
]


def create(
    db: Session,
    *,
    action: str,
    user_id: uuid.UUID | None,
    resource_type: str,
    resource_id: uuid.UUID,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
) -> AuditLog:
    """Insert an audit-log row and return the refreshed entity."""
    log = AuditLog(
        action=action,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        old_value=old_value,
        new_value=new_value,
    )
    db.add(log)
    db.flush()
    db.refresh(log)
    return log


def get_by_resource_id(db: Session, resource_id: uuid.UUID) -> list[AuditLog]:
    """Return all audit-log entries for *resource_id*, oldest first."""
    stmt = (
        select(AuditLog)
        .where(AuditLog.resource_id == resource_id)
        .order_by(AuditLog.created_at.asc())
    )
    return list(db.scalars(stmt).all())
