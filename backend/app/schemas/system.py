"""Pydantic DTOs for the ``/api/v1/system/*`` endpoints.

Dashboard's Service Health card consumes these — see
``docs/scheduling.md`` and ``notes/dashboard-implementation-plan.md`` for the
read-path design.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "CeleryStats",
    "DbPoolStats",
    "EndpointStat",
    "LatencyPercentiles",
    "RedMetricsResponse",
    "RedisStats",
    "ServiceHealthDetail",
    "ServiceHealthEntry",
    "SloComplianceResponse",
    "SystemHealthResponse",
    "SystemResourcesResponse",
    "UsernamesLookupResponse",
    "WorkerBreakdown",
    "WorkerStatus",
]

# Per-worker liveness vocabulary surfaced by ``GET /system/resources``.
#
# * ``"active"`` — worker process is responsive (``inspect().ping()`` reply)
#   AND/OR currently executing one or more tasks (``active_tasks > 0``).
# * ``"idle"`` — worker process is responsive but has no in-flight tasks.
#
# We do NOT model ``"dead"`` here: dead workers do not appear in
# ``inspect().registered()`` / ``ping()`` at all, so the only honest signal
# is "drop out of the workers[] array". The aggregate ``registered_workers``
# count reflects this implicitly.
WorkerStatus = Literal["active", "idle"]


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


class UsernamesLookupResponse(BaseModel):
    """UUID → username map returned by ``GET /system/usernames``.

    Keys are stringified UUIDs (so JSON round-trips cleanly); values are
    the matching ``users.username`` or ``None`` when the UUID is unknown
    (deleted user, typo, etc.). The frontend's Pending Ops table uses
    this to render a requester column without needing the root-only
    ``GET /users`` endpoint.
    """

    usernames: dict[str, str | None] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# /system/resources — USE (utilization / saturation / errors) read endpoint
# ---------------------------------------------------------------------------
#
# Plan A2 ships an operator-grade resources card so scheduler / root users can
# see DB pool pressure, Redis memory consumption, and Celery worker breakdown
# at a glance. Every section is nullable: when one probe fails (e.g. Celery
# down), the other two still populate and the endpoint returns 200 with
# ``celery: None`` rather than 5xx-ing the whole observability page.


class DbPoolStats(BaseModel):
    """SQLAlchemy ``QueuePool`` snapshot.

    ``utilization_pct`` is the high-signal field for the dashboard — it's the
    only one that lets a human spot pool exhaustion at a glance. Computed as
    ``checked_out / (size + max_overflow) * 100`` so 100% means every
    connection (steady-state + burst capacity) is in flight.
    """

    size: int
    checked_out: int
    overflow: int
    max_overflow: int
    utilization_pct: float


class RedisStats(BaseModel):
    """Redis ``INFO`` slice scoped to what the dashboard needs.

    All values come from ``redis.info()`` so types are coerced to ``int``
    at the service layer to insulate the DTO from server-version variance
    (some keys come back as bytes / strings depending on driver version).
    """

    used_memory_bytes: int
    used_memory_peak_bytes: int
    connected_clients: int
    ops_per_sec: int
    evicted_keys: int


class WorkerBreakdown(BaseModel):
    """Per-worker liveness + load row in the Celery resources block.

    See :data:`WorkerStatus` for status semantics. ``active_tasks`` is the
    length of ``inspect().active()[hostname]``; missing keys (the worker
    answered ``ping()`` but had no entry in ``active()``) are treated as 0.
    """

    hostname: str
    active_tasks: int
    status: WorkerStatus


class CeleryStats(BaseModel):
    """Celery worker fleet snapshot.

    ``workers`` is **always an array** when this object is non-null; an empty
    ``inspect()`` (no workers responded) yields ``workers: []``, NOT
    ``None``. The frontend can iterate without a null-check inside this
    section.

    ``truncated`` is a defensive flag for the 50-worker cap: if more than 50
    workers responded, the array is sliced to the first 50 (sorted by
    hostname) and ``truncated`` flips to ``True`` so the frontend can show
    "showing 50 of N" without inferring it from array length alone.
    """

    active_tasks: int
    queue_depth: int
    registered_workers: int
    workers: list[WorkerBreakdown] = Field(default_factory=list)
    truncated: bool = False


class SystemResourcesResponse(BaseModel):
    """Aggregate USE response for the observability page.

    Every section is independently nullable so a single probe failure
    degrades that section only — the endpoint stays 200 and the other
    sections still populate. The frontend treats ``None`` here as "we have
    no signal, hide this card" rather than as an error.
    """

    db_pool: DbPoolStats | None
    redis: RedisStats | None
    celery: CeleryStats | None


# ---------------------------------------------------------------------------
# /system/red — RED (rate / errors / duration) read endpoint
# ---------------------------------------------------------------------------
#
# Plan A1 ships the RED-metrics middleware + endpoint pair. The middleware
# writes one sample per request into the ``metrics:requests`` Redis ZSET; this
# endpoint reads the trailing window and aggregates rate / error percentage /
# latency percentiles for the dashboard. The shape below pins the wire
# contract the frontend depends on.


class LatencyPercentiles(BaseModel):
    """P50 / P95 / P99 / max latency in milliseconds for the window.

    Values are rounded to whole milliseconds: the dashboard renders them as
    integer pills (``"45 ms"``), so sub-millisecond precision is not useful
    and would just make the number harder to read.
    """

    p50: int
    p95: int
    p99: int
    max: int


class EndpointStat(BaseModel):
    """Per-endpoint slice surfaced in the RED endpoint's ``by_endpoint`` array.

    ``endpoint`` is the route template (e.g. ``GET /api/v1/orders/{order_id}``),
    NOT the raw URL — see ``app/core/red_metrics.py`` for why we template:
    raw URLs would explode the histogram into one bucket per UUID.
    """

    endpoint: str
    count: int
    error_pct: float
    p50_ms: int
    p95_ms: int
    p99_ms: int


class RedMetricsResponse(BaseModel):
    """Aggregated RED metrics for the dashboard's red-metrics card.

    Empty windows return zeros (not 404 / null) — the frontend prefers a
    stable envelope so it can keep rendering "0 req/s" instead of an
    error state.

    ``data_status`` distinguishes the two zero-envelope cases the
    aggregator can emit: ``"ok"`` (Redis healthy, no traffic in window)
    vs ``"degraded"`` (Redis unreachable / read failed — numbers are not
    live state). The frontend banner surfaces the degraded case so an
    operator does not mistake an outage for a quiet weekend. Corrupted
    individual JSON samples are not flagged here — they are skipped at
    decode time and the remaining samples still aggregate cleanly.
    """

    window_seconds: int
    total_requests: int
    rate_per_sec: float
    error_count: int
    error_pct: float
    latency_ms: LatencyPercentiles
    by_endpoint: list[EndpointStat] = Field(default_factory=list)
    data_status: Literal["ok", "degraded"] = "ok"


# ---------------------------------------------------------------------------
# /system/slo — SLO / error-budget read endpoint
# ---------------------------------------------------------------------------
#
# Plan A1b. Reuses the same ``metrics:requests`` ZSET as RED, just with a
# wider window. The frontend's fourth KPI card consumes this — see Plan B for
# the SLO colour-band rules.


class SloComplianceResponse(BaseModel):
    """SLO compliance + error-budget snapshot for the observability page.

    The underlying ZSET is trimmed to the last 1 hour by the RED
    middleware (see ``app/core/red_metrics.py``). When ``window_hours``
    exceeds the available retention, the response still reports against
    whatever samples are physically present; the
    ``data_window_seconds_actual`` field tells callers how much of the
    requested window is actually backed by data so the frontend can
    surface a "Showing 1h of data (requested 24h)" hint.

    ``data_window_seconds_actual`` is computed as
    ``min(requested_window_seconds, now - oldest_sample_ts)`` and is
    capped to the requested window. With no samples at all it is 0.
    """

    window_hours: int
    total_requests: int
    successful_requests: int
    success_pct: float
    slo_target_pct: float
    error_budget_pct_remaining: float
    error_budget_consumed_pct: float
    data_window_seconds_actual: int
    # See ``RedMetricsResponse.data_status`` — same semantics: ``"ok"``
    # means the empty / populated envelope is backed by a healthy Redis
    # read, ``"degraded"`` means we returned the empty envelope because
    # Redis was unreachable. SLO and RED share the same data source so
    # the two responses degrade together.
    data_status: Literal["ok", "degraded"] = "ok"
