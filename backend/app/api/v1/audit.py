"""Global audit-log feed router — backs the new ``/audit`` admin page.

This is the *cross-resource* sibling of:
  * ``GET /orders/{id}/audit-log`` — single-order timeline (in
    ``api/v1/orders.py``).
  * ``GET /users/{id}/audit`` — single-user timeline (in
    ``api/v1/users.py``).

The admin page that consumes this endpoint shows activity across every
actor / action / resource_type at once, with the kind of filter UX you'd
expect from a SIEM-lite view. RBAC is root-only: the audit log can
contain user metadata and resource snapshots, both of which are
sensitive — even scheduler is too broad an audience.

Per project convention, the route reads query params directly rather
than via ``Depends()``-bound DTO to keep the OpenAPI surface obvious.
Filters are forwarded as a typed :class:`AuditEventsFilters` to the
service so the repo signature stays clean.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_roles
from app.models.user import User, UserRole
from app.schemas.audit import AuditActionsResponse, AuditLogListResponse
from app.services import audit as audit_service
from app.services.audit import AuditEventsFilters

router = APIRouter()

_root_only = Depends(require_roles(UserRole.root))


@router.get("/events", response_model=AuditLogListResponse)
def list_audit_events(
    db: Session = Depends(get_db),
    current_user: User = _root_only,
    actor_id: uuid.UUID | None = Query(default=None),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    resource_id: uuid.UUID | None = Query(default=None),
    # ``from`` is a Python reserved word — bind via alias so the public
    # query name stays ``?from=...&to=...`` per the spec.
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> AuditLogListResponse:
    """Return a paginated, filtered slice of the global audit-log feed.

    Permission: root only — even scheduler is too broad an audience for
    a cross-resource activity timeline that may carry user PII /
    pre-mutation snapshots in ``old_value``.

    Query parameters (all optional, all filter-combine with AND):
        actor_id: only events emitted by this user UUID.
        action: exact match on the dotted event name (e.g.
            ``user.login_succeeded``). No prefix / fuzzy match — the
            frontend exposes a finite picker, so exact-match is enough.
        resource_type: e.g. ``user`` / ``order``.
        resource_id: only events for this resource UUID.
        from: inclusive lower bound on ``created_at`` (ISO-8601).
        to: exclusive upper bound on ``created_at`` — half-open
            ``[from, to)`` interval avoids the "is the last second
            included?" question.
        page: 1-indexed page number (``ge=1`` rejects 0 with 422).
        page_size: rows per page (``le=100`` rejects 101+ with 422).

    Ordering is fixed: ``created_at DESC, id DESC`` (newest first with
    a deterministic UUID tie-break for stable pagination when rows
    share a server-side timestamp).

    Errors:
        401: missing or invalid bearer token.
        403: authenticated user does not have the root role.
        422: ``page < 1`` or ``page_size`` outside ``[1, 100]``.
    """
    del current_user  # role check happens in the dependency — value unused.
    filters = AuditEventsFilters(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        from_ts=from_,
        to_ts=to,
        page=page,
        page_size=page_size,
    )
    return audit_service.get_events(db, filters)


@router.get("/actions", response_model=AuditActionsResponse)
def list_audit_actions(
    db: Session = Depends(get_db),
    current_user: User = _root_only,
) -> AuditActionsResponse:
    """Return the sorted, distinct set of audit ``action`` values in the DB.

    Backs the admin audit page's Action filter — replaces a hard-coded
    frontend constant (``KNOWN_AUDIT_ACTIONS``) that drifted out of sync
    with reality (missed ``user.update``, included ``schedule.*`` that no
    code emits). Sourcing the list from the live ``audit_logs`` table
    guarantees the autocomplete always matches what's actually present.

    Permission: root only — same RBAC as ``GET /audit/events``. The set
    of distinct actions is itself low-sensitivity, but exposing it to a
    broader audience would give a non-root caller a free reconnaissance
    surface ("which event types fire in this system?"), so we keep the
    audit feed's gate uniform across the two endpoints.

    Caching: none here. The DISTINCT scan against the indexed ``action``
    column is cheap on the small ``audit_logs`` table this project
    targets, and the frontend's TanStack Query layer already debounces
    repeat calls within a page session via ``staleTime: 60_000``. If
    table size grows by orders of magnitude later, a 60-second TTL
    cache (or Redis SET) would be the place to bolt that on.

    Response shape:
        ``{"actions": ["order.created", "user.login_succeeded", ...]}``
        ASC-sorted. Empty list (not 404) when ``audit_logs`` is empty.

    Errors:
        401: missing or invalid bearer token.
        403: authenticated user does not have the root role.
    """
    del current_user  # role check happens in the dependency — value unused.
    return audit_service.list_distinct_actions(db)
