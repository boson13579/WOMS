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
from app.schemas.audit import AuditLogListResponse
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
