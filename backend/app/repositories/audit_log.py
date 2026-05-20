"""Pure CRUD operations for the AuditLog entity."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.models.audit_log import AuditLog

__all__ = [
    "count_by_resource_id",
    "count_events",
    "create",
    "get_by_resource_id",
    "list_distinct_actions",
    "list_events",
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


def _events_filter_stmt(
    stmt: Select[Any],
    *,
    actor_id: uuid.UUID | None,
    action: str | None,
    resource_type: str | None,
    resource_id: uuid.UUID | None,
    from_ts: datetime | None,
    to_ts: datetime | None,
) -> Select[Any]:
    """Apply the shared global-feed filter set to *stmt* and return it.

    Kept private + parameterised on the base ``stmt`` so ``list_events`` and
    ``count_events`` cannot drift on the filter semantics (e.g. accidentally
    adding a filter to one but not the other and producing a paginator with
    a wrong ``total``).
    """
    if actor_id is not None:
        stmt = stmt.where(AuditLog.user_id == actor_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type is not None:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    if from_ts is not None:
        # Inclusive lower bound — typical "show events FROM X onwards" UX.
        stmt = stmt.where(AuditLog.created_at >= from_ts)
    if to_ts is not None:
        # Exclusive upper bound — half-open interval [from, to) is the
        # least-surprising convention for date ranges and avoids the
        # "is the last second included?" ambiguity of inclusive ranges.
        stmt = stmt.where(AuditLog.created_at < to_ts)
    return stmt


def list_events(
    db: Session,
    *,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
    oldest_first: bool = False,
) -> list[AuditLog]:
    """Return the paginated global audit-log feed for ``GET /audit/events``.

    All filter args are optional; passing none returns every row (paginated).
    Defaults match the global-feed UX (newest-first, page=1, page_size=20).

    Pagination uses the same ``(created_at, id)`` tie-breaker as
    :func:`get_by_resource_id` so seeding N rows in a single transaction
    (where ``func.now()`` collapses to one timestamp) still pages
    deterministically. ``id`` (UUIDv4) doesn't carry semantic order but it
    *does* total-order the rows, which is all we need.
    """
    stmt = _events_filter_stmt(
        select(AuditLog),
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        from_ts=from_ts,
        to_ts=to_ts,
    )
    if oldest_first:
        stmt = stmt.order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    else:
        stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    effective_page = max(page, 1)
    stmt = stmt.offset((effective_page - 1) * page_size).limit(page_size)
    return list(db.scalars(stmt).all())


def count_events(
    db: Session,
    *,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
) -> int:
    """Return the total row count for the global audit-log feed.

    Mirror of :func:`list_events`'s filter signature — kept in lockstep so
    a paginated response's ``total`` always matches the same filter the
    items page was drawn from. See :func:`_events_filter_stmt`.
    """
    stmt = _events_filter_stmt(
        select(func.count()).select_from(AuditLog),
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        from_ts=from_ts,
        to_ts=to_ts,
    )
    return int(db.scalar(stmt) or 0)


def list_distinct_actions(db: Session) -> list[str]:
    """Return the sorted, unique set of ``action`` values present in ``audit_logs``.

    Powers the ``GET /audit/actions`` admin endpoint, which the audit page
    uses to populate a dynamic typeahead (replacing the legacy hard-coded
    constant that drifted out of sync with reality).

    Sort order is ASC so the frontend can render the autocomplete list
    in a stable, predictable order without re-sorting client-side. The
    column is indexed (see ``audit_log.py`` model) and the resulting
    cardinality is small (the system emits a fixed vocabulary of dotted
    event names), so the DISTINCT scan is cheap enough that we don't
    cache here — let the route-level HTTP / FE query cache handle that.

    Returns an empty list when the table is empty rather than raising,
    so the FE's "no data yet" case can render an empty combobox without
    a special-case branch.
    """
    stmt = select(distinct(AuditLog.action)).order_by(AuditLog.action.asc())
    return list(db.scalars(stmt).all())
