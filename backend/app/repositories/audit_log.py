"""Pure CRUD operations for the AuditLog entity."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog

__all__ = [
    "count_by_resource_id",
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


def get_by_resource_id(
    db: Session,
    resource_id: uuid.UUID,
    *,
    resource_type: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    oldest_first: bool = True,
) -> list[AuditLog]:
    """Return audit-log entries for *resource_id* with optional filters.

    Args:
        db: SQLAlchemy session.
        resource_id: target resource UUID (e.g. order_id, user_id).
        resource_type: optional resource_type filter. When provided, defends
            against UUID-collision edge cases (e.g. someone seeds a user-id
            that happens to equal an order-id in tests). ``None`` preserves
            the pre-B3 behaviour of "all rows for this resource_id".
        page: 1-indexed page number. ``None`` (default) preserves the pre-B3
            "return everything" contract — required because
            ``services/order.py:get_audit_log`` and external callers rely on
            it. Pass ``1`` (with ``page_size``) for paginated access.
        page_size: rows per page. ``None`` (default) preserves the pre-B3
            "no LIMIT" contract; pass an integer to paginate. The API layer
            caps user-supplied values at 100.
        oldest_first: when True (default, pre-B3 behaviour) sorts by
            ``created_at ASC``; when False sorts DESC — the user audit-log
            endpoint wants newest first.

    Returns:
        List of ``AuditLog`` rows, sorted as requested. Defaults preserve
        the pre-B3 contract: bare ``get_by_resource_id(db, rid)`` returns
        all rows oldest-first (the existing order audit endpoint relies on
        this).
    """
    stmt = select(AuditLog).where(AuditLog.resource_id == resource_id)
    if resource_type is not None:
        stmt = stmt.where(AuditLog.resource_type == resource_type)

    # ``id`` (UUIDv4) is a stable secondary sort key when ``created_at``
    # ties at sub-microsecond resolution — every row inserted in the same
    # SQLAlchemy ``flush()`` gets the same ``func.now()`` server timestamp,
    # so without a tiebreaker the page-2 cut would be non-deterministic
    # and tests that seed N rows in one transaction become flaky. UUIDs
    # don't carry semantic order but they DO total-order the rows, which
    # is all we need to keep pagination stable.
    if oldest_first:
        stmt = stmt.order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    else:
        stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())

    # Pagination is opt-in: if neither knob was passed, return everything.
    # Setting only one is a programming error (we don't half-paginate); fall
    # back to "everything" rather than guess the missing value.
    if page is not None and page_size is not None:
        effective_page = max(page, 1)
        stmt = stmt.offset((effective_page - 1) * page_size).limit(page_size)

    return list(db.scalars(stmt).all())


def count_by_resource_id(
    db: Session,
    resource_id: uuid.UUID,
    *,
    resource_type: str | None = None,
) -> int:
    """Return the total number of audit rows matching the filter.

    Used by paginated endpoints to populate the ``total`` field of their
    list response. ``resource_type`` filter mirrors ``get_by_resource_id``
    so the pair stays in lockstep.
    """
    stmt = select(func.count()).select_from(AuditLog).where(AuditLog.resource_id == resource_id)
    if resource_type is not None:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    return int(db.scalar(stmt) or 0)
