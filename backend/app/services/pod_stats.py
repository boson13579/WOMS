"""Per-pod stats publish + cross-pod aggregate.

Multi-replica backend (2+ pods behind nginx LB) means any single ``/system/
resources`` request only sees its own pod's local state — DB pool checkout
count, WS connection count, etc. That's misleading for an operator: the
dashboard would flip-flop as nginx round-robins between pods.

Fix: every pod periodically publishes its local snapshot to Redis under
``pod_stats:<category>:<pod_id>`` with a short TTL (so a dead pod drops out
naturally). Aggregator SCANs the prefix and sums.

Publishing is done on-demand from inside the ``/system/resources`` handler
itself (publish then aggregate) — no background task, no lifespan plumbing.
With ~3s polling on the dashboard and 2 pods round-robin'd by nginx, both
pods refresh their snapshots within ~6s of any displayed value.
"""

from __future__ import annotations

import json
import os
import socket
import time
from functools import lru_cache
from typing import Any, cast
from urllib.parse import urlparse

import structlog
from redis import Redis

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

# TTL chosen so a pod that crashes or stops being polled drops out within
# ~30s. Long enough to survive the 3s dashboard poll cadence even with one
# pod being slow; short enough that a dead replica doesn't linger.
_TTL_SECONDS = 30

# Fast-fail pre-flight timeout for the Redis port check. 500 ms is the
# longest we accept blocking ``/system/resources`` while waiting for the
# OS to refuse the connection — tighter risks false negatives on
# laggy laptops; looser starts being a real UX regression.
_PORT_CHECK_TIMEOUT_SECONDS = 0.5

# Cache the reachability result briefly so a single ``/system/resources``
# request — which calls publish + aggregate for each category, currently
# four pod_stats calls per request — only pays the port-check once.
# A 2 s TTL is short enough that Redis recovery surfaces within ~2 s of
# the dashboard's next poll, and long enough to deduplicate within one
# poll cycle (3 s dashboard cadence).
_PORT_CHECK_CACHE_TTL_SECONDS = 2.0
_port_check_cache: dict[str, float] = {"checked_at": 0.0, "result": 0.0}


def _redis_port_open() -> bool:
    """Cheap reachability check before any Redis op.

    Without this, every ``publish_pod_stats`` / ``aggregate_pod_stats``
    call during a Redis outage adds the full client connect timeout
    (~2 s) to ``/system/resources``. Cached for
    ``_PORT_CHECK_CACHE_TTL_SECONDS`` so the four pod_stats calls per
    resources request only run the TCP check once.
    """
    now = time.monotonic()
    if now - _port_check_cache["checked_at"] < _PORT_CHECK_CACHE_TTL_SECONDS:
        return bool(_port_check_cache["result"])

    settings = get_settings()
    parsed = urlparse(str(settings.REDIS_URL))
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    ok = False
    try:
        addrs = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
        if addrs:
            family, socktype, proto, _, sockaddr = addrs[0]
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(_PORT_CHECK_TIMEOUT_SECONDS)
                sock.connect(sockaddr)
                ok = True
    except OSError:
        ok = False
    _port_check_cache["checked_at"] = now
    _port_check_cache["result"] = 1.0 if ok else 0.0
    return ok


@lru_cache(maxsize=1)
def get_pod_id() -> str:
    """Stable identifier for this process.

    Priority:
    1. ``POD_ID`` env var (k8s downward API can inject ``metadata.name``).
    2. ``HOSTNAME`` env var (Docker compose sets this to the container ID
       prefix automatically).
    3. ``socket.gethostname()`` (last-resort).

    Cached at process lifetime — pod id never changes after startup.
    """
    return os.environ.get("POD_ID") or os.environ.get("HOSTNAME") or socket.gethostname()


@lru_cache(maxsize=1)
def _redis() -> Redis:
    return Redis.from_url(
        str(get_settings().REDIS_URL),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _key(category: str, pod_id: str) -> str:
    return f"pod_stats:{category}:{pod_id}"


def publish_pod_stats(category: str, data: dict[str, Any]) -> None:
    """Write this pod's snapshot for *category* with the standard TTL.

    Categories used today: ``"db_pool"``, ``"ws"``. Add new keys here as
    new per-pod metrics show up.

    Best-effort: failures are logged at WARN and swallowed. The aggregator
    will fall back to the local snapshot if Redis is dead.
    """
    if not _redis_port_open():
        return  # Redis down — caller falls back to local snapshot
    try:
        rds = _redis()
        rds.set(
            _key(category, get_pod_id()),
            json.dumps(data),
            ex=_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("pod_stats.publish_failed", category=category, error=str(exc))


def aggregate_pod_stats(category: str) -> list[dict[str, Any]]:
    """Return a list of per-pod snapshots (each augmented with ``pod_id``).

    Each entry: ``{"pod_id": "<id>", **published_data}``.
    Returns empty list on Redis failure — callers should treat that as
    "fall back to local-only snapshot".
    """
    if not _redis_port_open():
        return []
    try:
        rds = _redis()
        prefix = f"pod_stats:{category}:"
        out: list[dict[str, Any]] = []
        # SCAN over the small fixed-prefix namespace; 100 batch is plenty
        # since pod count is small (<10 in normal deploys).
        for key in rds.scan_iter(f"{prefix}*", count=100):
            raw = cast("str | None", rds.get(key))
            if raw is None:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            pod_id = key[len(prefix) :]
            out.append({"pod_id": pod_id, **data})
        return out
    except Exception as exc:
        logger.warning("pod_stats.aggregate_failed", category=category, error=str(exc))
        return []
