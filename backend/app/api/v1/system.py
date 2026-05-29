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
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user, require_roles
from app.models.user import User, UserRole
from app.schemas.system import (
    RedMetricsResponse,
    ScheduleLagStats,
    SloComplianceResponse,
    SystemHealthResponse,
    SystemResourcesResponse,
    UsernamesLookupResponse,
)
from app.services import red_metrics as red_metrics_service
from app.services import schedule_lag as schedule_lag_service
from app.services.system import gather_resources, gather_system_health, lookup_usernames

router = APIRouter()

# Limit per-request UUID count. 100 covers the dashboard's top-N Pending
# Ops view (10) plus headroom for any future bulk widget; bigger requests
# are almost certainly a programming error (or abuse) on the caller side.
_MAX_USERNAME_LOOKUPS_PER_REQUEST = 100


@router.get(
    "/health",
    summary="Aggregated service health for the dashboard.",
)
def get_system_health(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
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
    "/resources",
    summary="USE (utilization / saturation / errors) snapshot of DB, Redis, Celery.",
)
def get_system_resources(
    current_user: Annotated[User, Depends(require_roles(UserRole.scheduler, UserRole.root))],
) -> SystemResourcesResponse:
    """Operator-grade resource snapshot for the observability page.

    Permission: scheduler + root only (viewer / order_manager → 403).
    Returns ``db_pool`` / ``redis`` / ``celery`` sections; each is
    independently nullable so a single probe failure degrades that
    section only — the endpoint still returns 200.
    """
    del current_user  # role check is done by ``require_roles`` — value not needed.
    return gather_resources()


@router.get(
    "/red",
    summary="RED (rate / errors / duration) metrics over a trailing window.",
)
def get_red_metrics(
    current_user: Annotated[User, Depends(require_roles(UserRole.scheduler, UserRole.root))],
    window_seconds: Annotated[
        int,
        Query(
            ge=1,
            description=(
                "Trailing window in seconds. The underlying ZSET is trimmed to "
                "the last 5 minutes, so windows wider than 300s return only the "
                "samples that are physically present (no error)."
            ),
        ),
    ] = 60,
) -> RedMetricsResponse:
    """Return aggregated RED metrics for the trailing window.

    Permission: scheduler + root only — RED data is operator-grade and
    not meaningful to viewers / order managers.

    Empty windows return zero values (not 404) so the dashboard can render
    a stable envelope. Window seconds outside the underlying 5-minute
    retention silently fall back to whatever samples exist.
    """
    del current_user  # role check is done by ``require_roles``; value unused.
    return red_metrics_service.compute_window(window_seconds)


@router.get(
    "/slo",
    summary="SLO compliance + error-budget snapshot over a trailing window.",
)
def get_slo_compliance(
    current_user: Annotated[User, Depends(require_roles(UserRole.scheduler, UserRole.root))],
    window_hours: Annotated[
        int,
        Query(
            ge=1,
            le=168,
            description=(
                "Trailing window in hours. The underlying ZSET is trimmed to "
                "the last 1 hour by the RED middleware, so longer windows "
                "report against the available sample slice rather than a true "
                "24h history. The response carries ``data_window_seconds_actual`` "
                "so the caller can see exactly how much of the requested window "
                "is actually backed by data."
            ),
        ),
    ] = 24,
) -> SloComplianceResponse:
    """Return SLO compliance + error-budget remaining for the trailing window.

    Permission: scheduler + root only (matches ``/system/red``).

    Empty windows report ``success_pct=100`` and full budget remaining
    — no traffic means no budget consumed. The response also carries
    ``data_window_seconds_actual`` so the frontend can surface a
    "Showing Xm of data (requested Yh)" hint when the requested window
    exceeds the underlying ZSET retention (1h).
    """
    del current_user
    return red_metrics_service.compute_slo(window_hours)


@router.get(
    "/schedule-lag",
    summary="P50 / P95 / max compound enqueue → commit latency over a window.",
)
def get_schedule_lag(
    current_user: Annotated[User, Depends(require_roles(UserRole.scheduler, UserRole.root))],
    window_seconds: Annotated[
        int,
        Query(
            ge=1,
            le=3600,
            description=(
                "Trailing window in seconds. The underlying sorted set is "
                "trimmed to 1 hour of retention — same upper bound as the "
                "widest pill (15m / 1h) on the observability page."
            ),
        ),
    ] = 60,
) -> ScheduleLagStats:
    """Return aggregated schedule-pipeline lag (enqueue → worker commit).

    Permission: scheduler + root only — matches the other observability
    endpoints. Empty windows return a zero envelope so the frontend can
    render "no samples yet" instead of erroring.
    """
    del current_user
    return schedule_lag_service.compute_window(window_seconds)


@router.get(
    "/usernames",
    summary="Bulk UUID → username lookup for dashboard rendering.",
)
def get_usernames(
    ids: Annotated[
        str,
        Query(
            min_length=1,
            description=(
                "Comma-separated list of user UUIDs to resolve. Up to "
                f"{_MAX_USERNAME_LOOKUPS_PER_REQUEST} per request."
            ),
        ),
    ],
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
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
