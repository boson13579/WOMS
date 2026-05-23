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
import uuid as uuid_module
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Literal, cast
from urllib.parse import urlparse

import structlog
from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_pool_stats
from app.models.user import User
from app.schemas.system import (
    CeleryStats,
    DbPoolPerReplica,
    DbPoolStats,
    RedisStats,
    ServiceHealthDetail,
    ServiceHealthEntry,
    SystemHealthResponse,
    SystemResourcesResponse,
    UsernamesLookupResponse,
    WorkerBreakdown,
    WorkerStatus,
    WsConnectionsPerReplica,
    WsConnectionStats,
)
from app.services.pod_stats import (
    aggregate_pod_stats,
    get_pod_id,
    publish_pod_stats,
)
from app.workers.celery_app import celery_app

ServiceId = Literal["api", "postgres", "redis", "celery"]
HealthStatus = Literal["healthy", "warning", "error"]

logger = structlog.get_logger(__name__)

__all__ = ["gather_resources", "gather_system_health", "lookup_usernames"]

# Defensive cap on the per-worker breakdown returned to the dashboard. Course-
# scale deployments won't get anywhere near this, but a misbehaving production
# fleet (or a test fixture) could theoretically register thousands of workers
# and balloon the JSON payload. Slicing the sorted-by-hostname list keeps the
# response bounded; the ``truncated`` flag on ``CeleryStats`` tells the
# frontend when it happened.
_MAX_WORKER_BREAKDOWN = 50

# Celery default queue name; matches ``celery_app.conf.task_default_queue``
# (Celery falls back to ``"celery"`` when nothing is set, which is exactly
# what ``app.workers.celery_app`` does today). Queue depth is read via
# ``LLEN`` because the Redis broker stores tasks as a plain list.
_CELERY_DEFAULT_QUEUE = "celery"

# Inspect timeout: Celery's default 1.0s blocks the resources endpoint when
# no worker registers (which is the production posture during a restart).
# ``inspect(timeout=0.5)`` is a broadcast-and-gather single deadline, not
# per-worker — adding workers does NOT scale the wall-clock cost.
_CELERY_INSPECT_TIMEOUT_SECONDS = 0.5


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

    Cached at process lifetime (``maxsize=1``) — the client instance is
    reused across every probe call. ``REDIS_URL`` is read from
    ``Settings`` once on the first invocation and pinned thereafter, so
    a runtime settings reload would not be picked up here. Production
    doesn't reload settings, and tests monkeypatch this whole accessor
    (not ``Settings``) to swap clients, which bypasses the cache.

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
        addrs = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
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
      ``failed`` + ``finished_at``). This records the **last task's
      outcome**, not the current worker health — important for the
      ``failed`` interpretation below.
    * ``schedule:pending_ops`` — sorted set of queued compounds. Used
      as one of the inputs to the stall detector below; deep queue
      by itself is **not** a warning signal (burst loads of 100s of
      compounds are normal; what matters is whether the worker is
      draining them).

    Severity mapping (queue-aware so the dashboard tile colour matches
    operational urgency rather than the literal status word):

    * ``state=failed`` AND ``queue_depth > 0`` → ``error`` (red): a
      task just failed AND there's still work waiting — actively broken.
    * ``state=failed`` AND ``queue_depth == 0`` → ``warning`` (yellow):
      a past task failed but nothing's queued, so the failure may be
      historical (e.g. a transient bug fixed by reset). Summary calls
      out the failure timestamp so an operator can decide.
    * ``state=idle`` + ``queue>0`` + last finish >
      ``_CELERY_STALL_THRESHOLD_SECONDS`` ago → ``warning`` (stall —
      worker dead with backlog).
    * Otherwise → ``healthy``.

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
    if worker_state == "failed" and queue_depth > 0:
        # Actively broken: a task just failed AND there's queued work
        # that won't drain until something intervenes.
        status = "error"
        summary = (
            f"Last run failed — {queue_depth} compound{'s' if queue_depth != 1 else ''} "
            f"still pending"
        )
    elif worker_state == "failed":
        # Past incident: a task failed, but nothing's queued so the
        # next caller may succeed. Surface the timestamp so operators
        # know if it's recent (urgent) or old (stale signal).
        status = "warning"
        if finished_at_raw:
            summary = f"Last run failed at {finished_at_raw} — no new tasks since"
        else:
            summary = "Last run failed — no new tasks since"
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


# ---------------------------------------------------------------------------
# /system/resources — DB pool + Redis info + Celery introspection
# ---------------------------------------------------------------------------


def _get_db_pool_stats() -> DbPoolStats | None:
    """Return a :class:`DbPoolStats` snapshot or ``None`` when unavailable.

    Delegates the heavy lifting to :func:`app.core.db.get_pool_stats` which
    knows the SQLAlchemy ``QueuePool`` internals. Catching the broader
    ``Exception`` here is intentional: the resources endpoint must NEVER
    fail because one probe blew up — we return ``None`` and let the
    frontend hide the section.
    """
    try:
        raw = get_pool_stats()
    except Exception as exc:
        logger.warning("system.resources.db_pool.probe_failed", error=str(exc))
        return None
    if raw is None:
        return None

    # Publish this pod's snapshot, then aggregate every published replica
    # from Redis. The current pod's snapshot is included via the publish
    # we just did, so single-replica deployments still see consistent
    # numbers; multi-replica deployments now see the cluster-wide sum.
    local = {
        "size": int(raw["size"]),
        "checked_out": int(raw["checked_out"]),
        "overflow": int(raw["overflow"]),
        "max_overflow": int(raw["max_overflow"]),
    }
    publish_pod_stats("db_pool", local)
    snapshots = aggregate_pod_stats("db_pool")

    # Fall back to local-only when Redis publish/aggregate failed —
    # better to show this pod's slice than nothing at all.
    if not snapshots:
        snapshots = [{"pod_id": get_pod_id(), **local}]

    replicas = [
        DbPoolPerReplica(
            pod_id=str(s["pod_id"]),
            size=int(s["size"]),
            checked_out=int(s["checked_out"]),
            overflow=int(s["overflow"]),
            max_overflow=int(s["max_overflow"]),
        )
        for s in snapshots
    ]
    # Sort for stable rendering — pod_id is the only intrinsic ordering
    # we have without dragging cluster topology in.
    replicas.sort(key=lambda r: r.pod_id)

    total_size = sum(r.size for r in replicas)
    total_checked_out = sum(r.checked_out for r in replicas)
    total_overflow = sum(r.overflow for r in replicas)
    total_max_overflow = sum(r.max_overflow for r in replicas)
    capacity = max(total_size + total_max_overflow, 1)
    utilization_pct = round(total_checked_out / capacity * 100, 1)

    return DbPoolStats(
        size=total_size,
        checked_out=total_checked_out,
        overflow=total_overflow,
        max_overflow=total_max_overflow,
        utilization_pct=utilization_pct,
        replicas=replicas,
    )


def _get_redis_stats() -> RedisStats | None:
    """Return a :class:`RedisStats` snapshot or ``None`` when Redis is unreachable.

    Reuses the cached client + port pre-flight from the existing health
    probes — same fast-fail behaviour when Redis is genuinely down.
    Calls ``info("memory")``, ``info("clients")``, and ``info("stats")``
    individually because asking for ``info()`` (all sections) is heavier
    than necessary and triggers Redis to compute extra slow stats.
    """
    if not _redis_port_open():
        return None
    try:
        rds = _get_redis_client()
        # ``redis-py`` type stubs return ``Awaitable | Any`` for ``info`` to
        # cover the async client too — we use the sync client, so cast to the
        # plain ``dict`` shape we know we get. Mirrors the existing pattern
        # in ``_probe_celery``.
        info_mem = cast("dict[str, Any]", rds.info("memory"))
        info_clients = cast("dict[str, Any]", rds.info("clients"))
        info_stats = cast("dict[str, Any]", rds.info("stats"))
    except Exception as exc:
        logger.warning("system.resources.redis.probe_failed", error=str(exc))
        return None
    try:
        return RedisStats(
            used_memory_bytes=int(info_mem.get("used_memory", 0)),
            used_memory_peak_bytes=int(info_mem.get("used_memory_peak", 0)),
            # ``maxmemory == 0`` is the Redis convention for "no cap"
            # (the docker / local default). Frontend uses this to decide
            # whether to render a saturation bar at all — without a cap
            # there is no meaningful denominator.
            max_memory_bytes=int(info_mem.get("maxmemory", 0)),
            connected_clients=int(info_clients.get("connected_clients", 0)),
            ops_per_sec=int(info_stats.get("instantaneous_ops_per_sec", 0)),
            evicted_keys=int(info_stats.get("evicted_keys", 0)),
        )
    except (TypeError, ValueError) as exc:
        # Defensive: if Redis returns a key with unexpected type we'd rather
        # null the section than 500. (Real Redis always returns ints here.)
        logger.warning("system.resources.redis.info_parse_failed", error=str(exc))
        return None


def _get_celery_stats() -> CeleryStats | None:
    """Return a :class:`CeleryStats` snapshot or ``None`` when introspection fails.

    Strategy:

    * Issue one ``inspect()`` with a 0.5s broadcast deadline and call
      ``.active()`` + ``.ping()`` on it. Each call still incurs the
      broadcast cost, but we avoid building three separate ``Inspect``
      objects (cheap object, but conceptually cleaner to share state).
    * Active task aggregate = ``sum(len(tasks) for tasks in active.values())``.
    * ``registered_workers`` is derived from the union of hostnames that
      responded to either ``active()`` or ``ping()``. Using ping as the
      source of truth (rather than ``registered()`` which lists task
      *names*) means we count actually-responsive workers.
    * Per-worker rows are sorted by hostname for stable rendering, then
      truncated to :data:`_MAX_WORKER_BREAKDOWN` with the ``truncated``
      flag set.
    * Queue depth from ``LLEN celery`` against the broker Redis. Falls
      back to 0 on any Redis error — celery section still serves with
      worker info, just with a possibly stale queue depth.
    """
    try:
        # ``celery_app`` is imported at module top — tests monkeypatch
        # ``app.services.system.celery_app`` to inject a fake. The Celery
        # app is otherwise cheap to import (already pulled in by the FastAPI
        # entrypoint via ``app.workers.scheduling``).
        inspector = celery_app.control.inspect(timeout=_CELERY_INSPECT_TIMEOUT_SECONDS)
        active = inspector.active() or {}
        ping = inspector.ping() or []
    except Exception as exc:
        logger.warning("system.resources.celery.inspect_failed", error=str(exc))
        return None

    # ``ping()`` returns a list of single-key dicts: [{"celery@h1": {"ok":"pong"}}, ...].
    # Some Celery versions return a dict keyed by hostname; handle both.
    ping_hostnames: set[str] = set()
    if isinstance(ping, list):
        for entry in ping:
            if isinstance(entry, dict):
                ping_hostnames.update(entry.keys())
    elif isinstance(ping, dict):
        ping_hostnames.update(ping.keys())

    # Union of every hostname we have any signal for.
    hostnames: set[str] = set()
    hostnames.update(active.keys())
    hostnames.update(ping_hostnames)

    workers: list[WorkerBreakdown] = []
    for hostname in sorted(hostnames):
        tasks = active.get(hostname) or []
        task_count = len(tasks) if isinstance(tasks, list) else 0
        # ``"active"`` when the worker has in-flight tasks. ``"idle"`` when
        # it's reachable (ping or active map presence) but has no tasks.
        # We never emit ``"dead"`` — dead workers drop out of both maps.
        status: WorkerStatus = "active" if task_count > 0 else "idle"
        workers.append(WorkerBreakdown(hostname=hostname, active_tasks=task_count, status=status))

    # Aggregate ``active_tasks`` from the source ``active`` map BEFORE
    # truncation so the headline number is honest even when we truncate
    # the per-worker rows.
    total_active = sum(len(tasks) if isinstance(tasks, list) else 0 for tasks in active.values())

    truncated = len(workers) > _MAX_WORKER_BREAKDOWN
    if truncated:
        workers = workers[:_MAX_WORKER_BREAKDOWN]

    # Queue depth: best-effort. Failing here only zeros out queue_depth, the
    # rest of the celery section is still useful.
    queue_depth = 0
    try:
        rds = _get_redis_client()
        raw_depth = cast("int", rds.llen(_CELERY_DEFAULT_QUEUE))
        queue_depth = int(raw_depth) if raw_depth is not None else 0
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("system.resources.celery.queue_depth_failed", error=str(exc))

    return CeleryStats(
        active_tasks=total_active,
        queue_depth=queue_depth,
        registered_workers=len(hostnames),
        workers=workers,
        truncated=truncated,
    )


def _get_ws_connection_stats() -> WsConnectionStats | None:
    """Aggregate WebSocket session count across backend replicas.

    Each backend pod owns its WS clients in-memory (see
    ``app/api/v1/websocket.py::ConnectionManager``); the local count is
    published to Redis here as a side-effect, then we sum across every
    published replica. Single-pod or Redis-down deployments still see
    a meaningful number via the local-only fallback.

    Returns ``None`` only when we couldn't import the manager (which
    only happens during certain test configurations) — production
    always returns a populated envelope.
    """
    # Local import: ``ConnectionManager`` lives in ``app.api.v1.websocket``
    # (the WS endpoint module), so a top-level ``from app.api.v1...``
    # would invert the api→services layering. Importing at call-time
    # keeps the layer dependency contained to this one function. Move
    # the manager into ``app.services.websocket`` if you want this
    # cleaner; for now noqa with rationale.
    try:
        from app.api.v1.websocket import get_connection_manager  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("system.resources.ws.import_failed", error=str(exc))
        return None

    local_count = get_connection_manager().total_connections()
    publish_pod_stats("ws", {"count": local_count})
    snapshots = aggregate_pod_stats("ws")
    if not snapshots:
        snapshots = [{"pod_id": get_pod_id(), "count": local_count}]

    replicas = [
        WsConnectionsPerReplica(pod_id=str(s["pod_id"]), count=int(s["count"])) for s in snapshots
    ]
    replicas.sort(key=lambda r: r.pod_id)

    return WsConnectionStats(
        total=sum(r.count for r in replicas),
        replicas=replicas,
    )


def gather_resources() -> SystemResourcesResponse:
    """Compose the USE resources response from each per-section probe.

    Each section is independent: a Redis outage blanks ``redis`` but
    ``db_pool`` and ``celery`` still populate. The endpoint stays 200
    in all cases — the frontend keeps rendering the surviving cards
    rather than blowing up the whole observability page.
    """
    return SystemResourcesResponse(
        db_pool=_get_db_pool_stats(),
        redis=_get_redis_stats(),
        celery=_get_celery_stats(),
        ws_connections=_get_ws_connection_stats(),
    )


# ---------------------------------------------------------------------------
# Username lookup
# ---------------------------------------------------------------------------


def lookup_usernames(
    db: Session,
    user_ids: Iterable[uuid_module.UUID],
) -> UsernamesLookupResponse:
    """Resolve each ``user_id`` to its ``username`` or ``None`` if unknown.

    Returns a stable map keyed by the stringified UUID — JSON-friendly
    and easy for the frontend to index by ``entry.requested_by``.

    Soft-deleted users would normally not appear here, but the existing
    ``users`` model has no ``is_deleted`` flag (deactivation flips
    ``is_active`` instead). Inactive users are still returned by name —
    the dashboard's Pending Ops view legitimately needs to render their
    historical compounds.
    """
    ids = list(user_ids)
    if not ids:
        return UsernamesLookupResponse(usernames={})

    rows = db.scalars(select(User).where(User.id.in_(ids))).all()
    found: dict[str, str | None] = {str(u.id): u.username for u in rows}

    # Fill in nulls for any IDs the DB didn't know about so the response
    # is shaped consistently — caller can iterate without ``.get(...)``.
    for user_id in ids:
        key = str(user_id)
        if key not in found:
            found[key] = None

    return UsernamesLookupResponse(usernames=found)
