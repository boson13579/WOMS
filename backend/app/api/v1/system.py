"""System-level read endpoints — backs the dashboard's Service Health card.

Separate from ``app/api/v1/health.py`` (which stays minimal for k8s liveness
probes). This module's ``GET /system/health`` is the *informative* variant:
it probes Postgres / Redis / Celery and returns per-service status so a
human dashboard can show a degraded-mode view rather than just up/down.

Any logged-in user can read this — including viewers, who would otherwise
have an empty dashboard. Operator-grade details (versions, latencies) are
fine to surface to viewers since they don't include secrets.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.system import SystemHealthResponse
from app.services.system import gather_system_health

router = APIRouter()


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
