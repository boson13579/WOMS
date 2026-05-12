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
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Literal, cast
from urllib.parse import urlparse

import structlog
from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
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

# Seconds since the last finished task above which a non-empty queue is
# treated as a stall (worker likely died mid-cycle). Mirrors the threshold
# the dashboard frontend uses in ``deriveScheduleDisplay`` — keeping the
# two layers consistent matters because the dashboard's Service Health
# pill and Schedule Status pill draw from these two probes and should
# agree about "stalled vs. healthy".
#
# Note: queue depth itself is NOT a warning signal. A burst-load workflow
# can legitimately push hundreds of compounds into the queue in a few
# seconds, and that's expected throughput — what matters is whether the
# worker is actually draining (state=running OR finished_at fresh). The
# stall check below covers the genuinely bad case.
_CELERY_STALL_THRESHOLD_SECONDS = 30


# ---------------------------------------------------------------------------
# Redis client accessor
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_redis_client() -> Redis:
    """Module-level Redis client; instantiated on first use.

    Separate accessor (not reusing ``schedule_queue._redis()``) so tests can
    monkeypatch ``app.services.system._get_redis_client`` to inject a fake
    without touching every other Redis-using module.

    Connect / socket timeouts are tight (2s) AND retries disabled
    because this client is only used by the dashboard's health probe.
    Default redis-py retries 3 times on connection error with backoff —
    when Redis is dead that adds ~10s of latency before the probe can
    answer ``status=error``, by which time the frontend's request
    timeout has already fired and the dashboard renders "Failed to
    load" instead of the (correct) degraded payload. ``Retry(NoBackoff(),
    0)`` keeps the retry machinery in the call stack (some redis-py
    code-paths still expect it) but performs zero additional attempts.
    """
    return Redis.from_url(
        str(get_settings().REDIS_URL),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
    )


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


def _redis_socket_target() -> tuple[str, int]:
    """Parse REDIS_URL into (host, port) for raw socket reachability tests."""
    parsed = urlparse(str(get_settings().REDIS_URL))
    return parsed.hostname or "localhost", parsed.port or 6379


def _redis_port_open(timeout_seconds: float = 0.5) -> bool:
    """Cheap pre-flight: is the Redis port even accepting connections?

    redis-py's retry / health-check machinery (and the OS-level connect
    retries on Windows in particular) inflates a single failed Redis
    call from "instant" to ~10 seconds. A one-shot socket connect with
    a tight timeout lets the probe fast-fail when Redis is down.

    Why explicit IPv4: on Windows ``localhost`` resolves to both
    ``::1`` (IPv6) and ``127.0.0.1`` (IPv4). ``socket.create_connection``
    tries IPv6 first; the Docker-published Redis binds only to IPv4, so
    IPv6 hits the full timeout (no RST, no listener) before falling
    back to IPv4. Explicitly using ``AF_INET`` skips the wasted IPv6
    attempt and keeps the probe under 200ms on a refused port.
    """
    host, port = _redis_socket_target()
    try:
        addrs = socket.getaddrinfo(
            host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
        if not addrs:
            return False
        family, socktype, proto, _, sockaddr = addrs[0]
        with socket.socket(family, socktype, proto) as sock:
            sock.settimeout(timeout_seconds)
            sock.connect(sockaddr)
            return True
    except OSError:
        return False


def _probe_redis() -> ServiceHealthEntry:
    """Probe Redis with ``PING`` and report latency."""
    if not _redis_port_open():
        raise ConnectionError("Redis port not reachable")
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
      ``failed`` + ``finished_at``). ``failed`` flips us to warning so the
      dashboard surfaces it immediately.
    * ``schedule:pending_ops`` — sorted set of queued compounds. Used
      only as one of the inputs to the stall detector below; deep queue
      by itself is **not** a warning signal (burst loads of 100s of
      compounds are normal; what matters is whether the worker is
      draining them).

    Stall detection (the case ``state=idle`` + ``queue>0`` + worker
    actually dead — see frontend ``deriveScheduleDisplay`` for the
    matching UX logic): if there's queued work but the last task
    finished more than ``_CELERY_STALL_THRESHOLD_SECONDS`` ago, flip
    to warning. This catches a crashed worker with backlog regardless
    of queue size.

    Any Redis exception → ``error``: better to flag "we have no signal"
    than to silently report healthy.
    """
    # Same pre-flight as ``_probe_redis``: if the port is dead we want
    # to surface ``error`` in ~1s, not wait for redis-py's connect /
    # retry machinery to give up.
    if not _redis_port_open():
        return ServiceHealthEntry(
            id="celery",
            name="Celery Worker",
            status="error",
            summary="Unable to read scheduler state from Redis (port not reachable)",
            details=[ServiceHealthDetail(label="Error", value="Redis port not reachable")],
        )
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
    finished_at_raw = (status_doc or {}).get("finished_at")
    seconds_since_finish = _seconds_since(finished_at_raw)

    status: HealthStatus
    if worker_state == "failed":
        status = "warning"
        summary = "Scheduler reports state=failed"
    elif (
        worker_state == "idle"
        and queue_depth > 0
        and seconds_since_finish >= _CELERY_STALL_THRESHOLD_SECONDS
    ):
        status = "warning"
        summary = (
            f"Queue has {queue_depth} pending but no task in "
            f"{int(seconds_since_finish)}s — worker may be stuck"
        )
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


def _seconds_since(iso_timestamp: str | None) -> float:
    """Seconds between *iso_timestamp* and now (UTC).

    Returns ``+inf`` for missing / unparseable input so callers naturally
    treat "no signal" as the worst case.
    """
    if not iso_timestamp:
        return float("inf")
    try:
        # ``fromisoformat`` handles both naive and aware ISO strings on
        # Python 3.11+; the trailing 'Z' shorthand is normalised to
        # ``+00:00`` for older interpreter support.
        normalized = iso_timestamp.replace("Z", "+00:00")
        finished = datetime.fromisoformat(normalized)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(tz=UTC) - finished).total_seconds())
    except ValueError:
        return float("inf")


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
