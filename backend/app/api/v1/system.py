"""System-level read endpoints — backs the dashboard's Service Health card.

Separate from ``app/api/v1/health.py`` (which stays minimal for k8s liveness
probes). This module's ``GET /system/health`` is the *informative* variant:
it probes Postgres / Redis / Celery and returns per-service status so a
human dashboard can show a degraded-mode view rather than just up/down.

``GET /system/usernames`` is a slim public-readable UUID→username lookup
so dashboard widgets (e.g. Pending Ops) can render requester names without
needing the root-only ``GET /users`` endpoint.

Any logged-in user can read this — including viewers, who would otherwise
have an empty dashboard. Operator-grade details (versions, latencies) are
fine to surface to viewers since they don't include secrets.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.system import SystemHealthResponse, UsernamesLookupResponse
from app.services.system import gather_system_health, lookup_usernames

router = APIRouter()

# Limit per-request UUID count. 100 covers the dashboard's top-N Pending
# Ops view (10) plus headroom for any future bulk widget; bigger requests
# are almost certainly a programming error (or abuse) on the caller side.
_MAX_USERNAME_LOOKUPS_PER_REQUEST = 100


@router.get(
    "/health",
    response_model=SystemHealthResponse,
    summary="Aggregated service health for the dashboard.",
)
def get_system_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SystemHealthResponse:
    """Return a 4-entry list of (api, postgres, redis, celery) health snapshots.

    Permission: any logged-in user (no role gate).

    Response stays 200 even when individual probes fail — per-service
    ``status`` carries the bad news. The frontend treats one failing
    service as a local concern, not a request-level error.
    """
    del current_user  # FastAPI dependency is the authn check — value unused.
    return gather_system_health(db)


@router.get(
    "/usernames",
    response_model=UsernamesLookupResponse,
    summary="Bulk UUID → username lookup for dashboard rendering.",
)
def get_usernames(
    ids: str = Query(
        ...,
        min_length=1,
        description=(
            "Comma-separated list of user UUIDs to resolve. Up to "
            f"{_MAX_USERNAME_LOOKUPS_PER_REQUEST} per request."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UsernamesLookupResponse:
    """Return ``{uuid: username | null}`` for each requested ID.

    Permission: any logged-in user (no role gate). Username is treated as
    operator-grade info — the existing ``/users`` endpoint is root-only
    for the full record, but a name-only lookup is fine to expose so
    dashboard widgets can display requester names without leaking
    sensitive fields (email / role / etc.).

    Behaviour:
        * Unknown UUID → mapped to ``null`` (caller distinguishes
          missing rows from a 4xx error and can keep rendering).
        * Duplicate IDs in the request are deduped silently.
        * Empty / malformed / over-limit ``ids`` → 422 via the unified
          error envelope.
    """
    del current_user  # authn only; value not needed.

    raw_parts = [part.strip() for part in ids.split(",") if part.strip()]
    if not raw_parts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Query parameter `ids` must contain at least one UUID.",
        )
    if len(raw_parts) > _MAX_USERNAME_LOOKUPS_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Query parameter `ids` accepts at most "
                f"{_MAX_USERNAME_LOOKUPS_PER_REQUEST} UUIDs per request "
                f"(got {len(raw_parts)})."
            ),
        )

    try:
        unique_ids = list(dict.fromkeys(uuid.UUID(s) for s in raw_parts))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Query parameter `ids` contains an invalid UUID: {exc}",
        ) from exc

    return lookup_usernames(db, unique_ids)
