"""Health-check endpoint.

Exposed at `GET /api/v1/health`. Used by:
  * Docker / Kubernetes liveness probes.
  * GitHub Actions CI smoke tests.
  * Frontend "is the API up?" splash-screen check.

Kept intentionally minimal — no DB ping here. A *deep* health check that
verifies Postgres + Redis lives at `/api/v1/health/ready` (added in Phase 2).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    """Liveness response payload."""

    status: Literal["ok"]


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    response_description="Service is up and accepting requests.",
)
def get_health() -> HealthResponse:
    """Return a static OK so probes can detect process liveness."""
    return HealthResponse(status="ok")
