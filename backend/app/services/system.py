"""System-health probes for the dashboard's Service Health card.

Probes each dependency we care about (Postgres / Redis / Celery worker
state via Redis) and packages the result into a flat list of
:class:`ServiceHealthEntry`. The endpoint at ``app/api/v1/system.py`` is a
thin shim over :func:`gather_system_health`.

Design contract:

* Every individual probe is wrapped so a single failure surfaces as a
  ``status="error"`` entry — the overall HTTP response stays 200 so a
  degraded dashboard still renders the rest of the page.
* Probes report **latency** in ``details`` whenever they can (cheap to
  compute and very useful operationally).
* The Celery probe is interpretive: it can't observe the worker
  process directly, only the Redis state the worker maintains
  (``schedule:status`` and ``schedule:pending_ops``). That's enough to
  distinguish "scheduler is healthy", "queue depth is mounting", and
  "we have no signal at all".
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any, Literal, cast

import structlog
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.schemas.system import (
    ServiceHealthDetail,
    ServiceHealthEntry,
    SystemHealthResponse,
)

ServiceId = Literal["api", "postgres", "redis", "celery"]
HealthStatus = Literal["healthy", "warning", "error"]

logger = structlog.get_logger(__name__)

__all__ = ["gather_system_health"]


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
#
# Kept as module-level constants rather than env vars: these are operator-
# facing UX knobs (when does the dashboard turn yellow), not deploy-varying
# infrastructure. If a deployment really needs a different threshold the
# dashboard component can be told to ignore it. Promote to ``Settings`` if
# we ever grow a real reason to vary per-environment.

# Pending compound count above which Celery's status flips to "warning".
# 50 is well above the queue depth a healthy worker chews through in a
# minute even at production load and well below "something is genuinely
# stuck" depths (hundreds-to-thousands).
_CELERY_QUEUE_WARNING_THRESHOLD = 50


# ---------------------------------------------------------------------------
# Redis client accessor
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_redis_client() -> Redis:
    """Module-level Redis client; instantiated on first use.

    Separate accessor (not reusing ``schedule_queue._redis()``) so tests can
    monkeypatch ``app.services.system._get_redis_client`` to inject a fake
    without touching every other Redis-using module.
    """
    return Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True)


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def _probe_api() -> ServiceHealthEntry:
    """API service is healthy by definition — if we're answering, we're up."""
    settings = get_settings()
    return ServiceHealthEntry(
        id="api",
        name="API",
        status="healthy",
        summary=f"FastAPI · v{settings.APP_VERSION}",
        details=[
            ServiceHealthDetail(label="Version", value=settings.APP_VERSION),
            ServiceHealthDetail(label="Environment", value=settings.APP_ENV),
        ],
    )


def _probe_postgres(db: Session) -> ServiceHealthEntry:
    """Probe Postgres by running ``SELECT 1`` and reporting latency.

    Caller wraps this in a try/except — failure here means an exception
    bubbles up and the wrapper synthesises a ``status="error"`` entry.
    """
    start = time.perf_counter()
    db.execute(text("SELECT 1"))
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    return ServiceHealthEntry(
        id="postgres",
        name="PostgreSQL",
        status="healthy",
        summary="postgres:15-alpine",
        details=[
            ServiceHealthDetail(label="Latency", value=f"{elapsed_ms} ms"),
        ],
    )


def _probe_redis() -> ServiceHealthEntry:
    """Probe Redis with ``PING`` and report latency."""
    rds = _get_redis_client()
    start = time.perf_counter()
    rds.ping()
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    return ServiceHealthEntry(
        id="redis",
        name="Redis",
        status="healthy",
        summary="redis:7-alpine · cache + broker",
        details=[
            ServiceHealthDetail(label="Latency", value=f"{elapsed_ms} ms"),
        ],
    )


def _probe_celery() -> ServiceHealthEntry:
    """Probe the scheduler worker state via the Redis keys it maintains.

    We can't introspect the Celery worker process from here, so we read
    the two Redis surfaces it writes:

    * ``schedule:status`` — lifecycle JSON (``idle`` / ``running`` /
      ``failed``). ``failed`` flips us to warning so the dashboard
      surfaces it immediately.
    * ``schedule:pending_ops`` — sorted set of queued compounds. A deep
      queue (over ``_CELERY_QUEUE_WARNING_THRESHOLD``) flips us to
      warning even if status is otherwise fine, since that's the symptom
      of a worker not keeping up.

    Any Redis exception → ``error``: better to flag "we have no signal"
    than to silently report healthy.
    """
    rds = _get_redis_client()
    try:
        queue_depth = cast("int", rds.zcard("schedule:pending_ops"))
        status_raw = cast("str | None", rds.get("schedule:status"))
    except Exception as exc:
        logger.warning("system.health.celery.probe_failed", error=str(exc))
        return ServiceHealthEntry(
            id="celery",
            name="Celery Worker",
            status="error",
            summary="Unable to read scheduler state from Redis",
            details=[
                ServiceHealthDetail(label="Error", value=str(exc)),
            ],
        )

    status_doc: dict[str, Any] | None = None
    if status_raw:
        try:
            status_doc = json.loads(status_raw)
        except json.JSONDecodeError:
            logger.warning("system.health.celery.bad_status_doc", raw=status_raw)

    worker_state = (status_doc or {}).get("state", "idle")
    status: HealthStatus
    if worker_state == "failed":
        status = "warning"
        summary = "Scheduler reports state=failed"
    elif queue_depth > _CELERY_QUEUE_WARNING_THRESHOLD:
        status = "warning"
        summary = f"Queue depth {queue_depth} above threshold"
    else:
        status = "healthy"
        summary = f"Scheduler state={worker_state}"

    return ServiceHealthEntry(
        id="celery",
        name="Celery Worker",
        status=status,
        summary=summary,
        details=[
            ServiceHealthDetail(label="State", value=worker_state),
            ServiceHealthDetail(label="Queue depth", value=str(queue_depth)),
        ],
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _safe(
    probe_id: ServiceId,
    probe_name: str,
    runner: Callable[[], ServiceHealthEntry],
) -> ServiceHealthEntry:
    """Run *runner*, packaging any exception as an ``error`` entry.

    Keeps the main composition flat: one probe failing must not break
    the others, and the endpoint stays 200.
    """
    try:
        return runner()
    except Exception as exc:
        logger.warning(
            "system.health.probe_failed",
            probe=probe_id,
            error=str(exc),
        )
        return ServiceHealthEntry(
            id=probe_id,
            name=probe_name,
            status="error",
            summary=f"Probe failed: {exc}",
            details=[ServiceHealthDetail(label="Error", value=str(exc))],
        )


def gather_system_health(db: Session) -> SystemHealthResponse:
    """Run every service probe and assemble the dashboard response.

    Order of services in the response is fixed (api / postgres / redis /
    celery) so the frontend can rely on it.
    """
    services = [
        _safe("api", "API", _probe_api),
        _safe("postgres", "PostgreSQL", lambda: _probe_postgres(db)),
        _safe("redis", "Redis", _probe_redis),
        _safe("celery", "Celery Worker", _probe_celery),
    ]
    return SystemHealthResponse(services=services)
