"""Audit-log read service — backs the global ``GET /audit/events`` admin feed.

Per-resource audit history reads (``GET /orders/{id}/audit-log``,
``GET /users/{id}/audit``) currently live in their respective per-resource
services (``services/order.py``, ``services/user.py``). This module exists for
the cross-resource case: an admin viewing the activity timeline across every
actor / action / resource_type at once.

Thin wrapper — the heavy filtering lives in
``repositories/audit_log.py:list_events`` and ``count_events``. Service layer
exists to:

* Normalise the request DTO (``AuditEventsFilters``) into the repository
  signature so the route can stay a one-liner.
* Combine the page + count queries into the paginated response envelope.
* Provide a stable seam if we later add caching / authorization shaping
  here (e.g. masking ``new_value`` fields for non-root actors). Right now
  the route is root-only so no shaping is needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.repositories import audit_log as audit_log_repo
from app.schemas.audit import AuditActionsResponse, AuditLogListResponse, AuditLogResponse


class AuditEventsFilters(BaseModel):
    """Filter set for the global ``GET /audit/events`` feed.

    Mirrors the route's query-string contract so the service layer takes a
    single typed object rather than a long positional / keyword list. All
    fields are optional; absent fields fall through as ``None`` and skip
    the corresponding repository filter.

    Pagination knobs live on this object too (``page`` / ``page_size``) so
    a single ``Depends()`` resolves both the filter set and the page —
    matches the project's existing pattern (see ``OrderListResponse`` /
    ``list_orders`` for the parallel).
    """

    actor_id: uuid.UUID | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: uuid.UUID | None = None
    from_ts: datetime | None = None
    to_ts: datetime | None = None
    page: int = 1
    page_size: int = 20


def get_events(
    db: Session,
    filters: AuditEventsFilters,
) -> AuditLogListResponse:
    """Return the paginated, filtered global audit feed.

    Args:
        db: active SQLAlchemy session.
        filters: typed filter + pagination knobs (see
            :class:`AuditEventsFilters`). All fields optional; empty
            filters return every row (still paginated).

    Returns:
        :class:`AuditLogListResponse` with ``items`` for the requested
        page and the total ``total`` count for the SAME filter set so
        the frontend's paginator can compute page count without a
        second roundtrip.

    Notes:
        * RBAC (root-only) is enforced at the route layer via
          ``require_roles(UserRole.root)`` — this service does not
          re-check the actor. Per project convention, services trust
          the route's role-gate.
        * Sort is fixed at ``created_at DESC, id DESC`` (newest first
          with a deterministic tie-break). Not surfaced as a parameter
          — the admin feed UX has no use case for the inverse and
          flexibility is YAGNI.
    """
    items = audit_log_repo.list_events(
        db,
        actor_id=filters.actor_id,
        action=filters.action,
        resource_type=filters.resource_type,
        resource_id=filters.resource_id,
        from_ts=filters.from_ts,
        to_ts=filters.to_ts,
        page=filters.page,
        page_size=filters.page_size,
        oldest_first=False,
    )
    total = audit_log_repo.count_events(
        db,
        actor_id=filters.actor_id,
        action=filters.action,
        resource_type=filters.resource_type,
        resource_id=filters.resource_id,
        from_ts=filters.from_ts,
        to_ts=filters.to_ts,
    )
    return AuditLogListResponse(
        items=[AuditLogResponse.model_validate(row) for row in items],
        total=total,
        page=filters.page,
        page_size=filters.page_size,
    )


def list_distinct_actions(db: Session) -> AuditActionsResponse:
    """Return the sorted unique set of audit ``action`` values currently in the DB.

    Thin envelope around :func:`audit_log_repo.list_distinct_actions` — the
    service layer exists so the route stays a one-liner and so we have a
    stable seam for any future shaping (e.g. filtering deprecated actions
    out of the autocomplete). The repository sorts ASC; we preserve that.

    No cache layer here: the underlying DISTINCT scan on an indexed,
    low-cardinality column is fast on the small ``audit_logs`` table this
    project targets, and the frontend's ``staleTime: 60_000`` already
    debounces repeat calls within a page session.
    """
    actions = audit_log_repo.list_distinct_actions(db)
    return AuditActionsResponse(actions=actions)
