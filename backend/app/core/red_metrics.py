"""RED-metrics ingestion middleware.

Plan A1 — every HTTP request emits a sample carrying ``(path_template,
method, status, duration_ms, ts_ms)`` into a Redis sorted set
(``metrics:requests``) so the dashboard's RED card can aggregate rate /
error percent / latency percentiles over a rolling window. The aggregating
side lives in :mod:`app.services.red_metrics`; this file is just the
*ingestion* path.

Design contract:

* **Best-effort writes** — any Redis exception is caught and logged at
  ``warning`` so a broken/cache-cold Redis never degrades user-facing
  responses. The middleware MUST NOT raise into the response path.
* **Path templating** — we record the FastAPI route template
  (``/api/v1/orders/{order_id}``) instead of the raw URL so the histogram
  doesn't explode into one bucket per UUID. When no route matched (404s)
  we fall back to the raw path so weird traffic still shows up.
* **Memory bound** — every successful write opportunistically trims the
  ZSET to the last 1 hour via ``ZREMRANGEBYSCORE``. This is idempotent
  and constant-cost: at our coursework scale (~10 RPS sustained) the
  trimmer keeps the set under ~36k members (~2.9 MB at 80 bytes/member);
  a 1000 RPS sustained load would reach ~290 MB which is acceptable.
  The wider window lets the ``/system/slo`` endpoint surface a more
  meaningful sample slice than the previous 5-minute cap.
* **Async fire-and-forget** — the write uses ``asyncio.create_task`` so
  the response body finishes flushing without waiting for Redis.

Mounting order in :mod:`app.main` matters: this middleware is added AFTER
``correlation_id_middleware`` so the contextvar is already populated when
samples are recorded (samples don't currently embed ``trace.id`` but the
log lines emitted on error must carry it).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

import structlog
from fastapi import Request, Response
from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

# Redis key holding the rolling request-sample sorted set. Score = ts_ms,
# member = compact JSON document (see ``_record_sample`` for shape).
METRICS_KEY = "metrics:requests"

# Memory bound: trim every write to the last 1 hour. Widened from 5 min
# so that the ``/system/slo`` endpoint (default 24h window) reports a
# more representative sample slice. At ~10 RPS sustained this is ~36k
# members (~2.9 MB); even a 1000 RPS sustained load (~3.6M members,
# ~290 MB) is acceptable for the coursework scope. The SLO endpoint
# also surfaces ``data_window_seconds_actual`` so callers can see how
# much of their requested window is actually backed by data.
RETENTION_MS = 60 * 60 * 1000

# Paths we don't want in the histogram. The exact-match set covers
# observability self-traffic + the stdlib OpenAPI / docs surfaces; the
# suffix tuple covers OpenAPI under any API prefix (``/api/v1/openapi.json``);
# the prefix tuple covers ``/static`` and ``/favicon`` (browsers spam these
# on every page load and they would skew the histogram).
SKIP_EXACT: frozenset[str] = frozenset(
    [
        "/api/v1/system/health",
        "/api/v1/system/red",
        "/api/v1/system/resources",
        "/api/v1/system/slo",
        "/api/v1/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    ]
)
SKIP_PREFIXES: tuple[str, ...] = ("/static", "/favicon")
# Path suffixes — captures OpenAPI / docs surfaces regardless of where
# they're mounted (``/api/v1/openapi.json``, ``/openapi.json``).
SKIP_SUFFIXES: tuple[str, ...] = ("/openapi.json", "/docs", "/redoc")

_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


@lru_cache(maxsize=1)
def _get_metrics_redis() -> Redis:
    """Module-level Redis client used by the middleware writer.

    Cached at process lifetime — connection pool is shared across every
    sample. Tests monkeypatch this accessor (not ``Settings``) so a fake
    Redis can be injected without touching every other Redis-using module.

    Retries disabled + tight timeouts: the metrics path is hot; we'd rather
    drop a sample than block the next request waiting on a dead Redis.
    """
    return Redis.from_url(
        str(get_settings().REDIS_URL),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
    )


def _should_skip(request: Request) -> bool:
    """Return True when *request* should NOT produce a RED sample.

    Skipped traffic:
      * ``OPTIONS`` preflight (CORS noise).
      * Self-traffic from the observability endpoints (so polling
        ``/system/red`` does not inflate its own histogram).
      * OpenAPI / docs surfaces.
      * Static assets and the favicon (any path under ``/static`` or
        starting with ``/favicon``).
    """
    if request.method == "OPTIONS":
        return True
    path = request.url.path
    if path in SKIP_EXACT:
        return True
    if any(path.startswith(prefix) for prefix in SKIP_PREFIXES):
        return True
    return any(path.endswith(suffix) for suffix in SKIP_SUFFIXES)


def _route_template(request: Request) -> str:
    """Return the FastAPI route template for *request* or a fixed no-route bucket.

    Why template: ``/orders/{order_id}`` is one bucket regardless of the
    actual UUID. Raw paths would explode the per-endpoint histogram into
    thousands of one-hit rows and ruin both the top-10 view and Redis
    memory.

    Why ``"(no route)"`` for unmatched requests: returning ``request.url.path``
    here is a high-cardinality injection vector. An attacker (or a buggy
    crawler) probing ``/random-uuid-1``, ``/random-uuid-2``, ... would create
    one fresh bucket per URL, blowing up ``by_endpoint`` and bloating the
    ``metrics:requests`` ZSET (which is otherwise memory-bound only by
    :data:`RETENTION_MS`). Collapsing all unmatched paths into a single
    ``"(no route)"`` bucket preserves visibility of "we are seeing 404 noise"
    without giving the caller any ability to enumerate buckets.
    """
    route = request.scope.get("route")
    template: Any = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return "(no route)"


async def _record_sample(
    *,
    path: str,
    method: str,
    status: int,
    duration_ms: float,
    ts_ms: int,
) -> None:
    """Best-effort persist of one request sample to Redis.

    Two operations executed via a pipeline:
      1. ``ZADD metrics:requests {ts_ms} {json}`` — append the sample.
      2. ``ZREMRANGEBYSCORE metrics:requests -inf {cutoff}`` — trim to the
         last :data:`RETENTION_MS` milliseconds.

    Member is compact JSON with short field names (``p`` / ``m`` / ``s`` /
    ``d`` / ``t``) to halve memory vs verbose names — at 1000 RPS those
    bytes add up. ``ts_ms`` is included inside the JSON too so identical
    same-millisecond samples don't collide on the sorted-set member key.

    Any exception is caught + logged at warning. Crucially this never
    raises into the response path — the middleware that schedules this
    task ignores its result.
    """
    try:
        # Compact JSON: short keys + no whitespace separator. ``p``/``m``
        # carry the endpoint identity; ``s`` is HTTP status; ``d`` is
        # latency in ms; ``t`` is the timestamp (also used as the ZSET
        # score, repeated here so the member is unique even if two
        # samples land in the same millisecond).
        member = json.dumps(
            {"p": path, "m": method, "s": status, "d": round(duration_ms, 2), "t": ts_ms},
            separators=(",", ":"),
        )
        cutoff = ts_ms - RETENTION_MS
        rds = _get_metrics_redis()
        pipe = rds.pipeline(transaction=False)
        pipe.zadd(METRICS_KEY, {member: ts_ms})
        pipe.zremrangebyscore(METRICS_KEY, "-inf", f"({cutoff}")
        pipe.execute()
    except Exception as exc:
        # Best-effort by contract: a broken Redis must not propagate into
        # the response path. Logged + swallowed.
        logger.warning("red_metrics.record_failed", error=str(exc))


async def red_metrics_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """FastAPI middleware that emits a RED sample for every non-skipped request.

    Even when the downstream handler raises, the ``finally`` block still
    schedules the sample (with ``status_code=500``) so error spikes are
    visible in the histogram. Cancellation during dispatch is
    re-raised — we don't want to silently absorb a client disconnect.
    """
    if _should_skip(request):
        return await call_next(request)

    start = time.perf_counter()
    status_code = 500
    response: Response | None = None
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        # Fire-and-forget. ``create_task`` schedules the write on the
        # current event loop; the coroutine itself swallows every
        # exception so an unawaited-task warning is impossible — and the
        # response can flush without waiting for Redis.
        try:
            task = asyncio.create_task(
                _record_sample(
                    path=_route_template(request),
                    method=request.method,
                    status=status_code,
                    duration_ms=elapsed_ms,
                    ts_ms=int(time.time() * 1000),
                )
            )
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
        except RuntimeError as exc:
            # No running event loop (e.g. inside ``TestClient`` finalize).
            # Swallow + log — middleware MUST NOT raise.
            logger.warning("red_metrics.schedule_failed", error=str(exc))
