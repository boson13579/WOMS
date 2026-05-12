"""Pydantic DTOs for the ``/api/v1/system/*`` endpoints.

Dashboard's Service Health card consumes these — see
``docs/scheduling.md`` and ``notes/dashboard-implementation-plan.md`` for the
read-path design.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "ServiceHealthDetail",
    "ServiceHealthEntry",
    "SystemHealthResponse",
]


class ServiceHealthDetail(BaseModel):
    """One label/value pair displayed under the status pill.

    Kept deliberately string-typed so the same DTO can carry latency
    ("2 ms"), counters ("12 / 100 conns"), and version strings. The
    frontend doesn't compute on these values — it just renders them.
    """

    label: str
    value: str


class ServiceHealthEntry(BaseModel):
    """Health snapshot of a single dependency.

    The ``id`` is the stable machine name (``"api"`` / ``"postgres"`` /
    ``"redis"`` / ``"celery"``); ``name`` is the human label rendered by
    the dashboard. ``status`` follows a small traffic-light vocabulary
    so the frontend can colour the pill without branching on free-text.
    """

    id: Literal["api", "postgres", "redis", "celery"]
    name: str
    status: Literal["healthy", "warning", "error"]
    summary: str
    details: list[ServiceHealthDetail] = Field(default_factory=list)


class SystemHealthResponse(BaseModel):
    """Aggregated health for the four dashboard-tracked services.

    Order matches the dashboard's Service Health grid (api, postgres,
    redis, celery) so the frontend can rely on positional indexing if
    convenient. The grid degrades gracefully if any single service
    reports ``error`` — the overall response is still 200.
    """

    services: list[ServiceHealthEntry]
