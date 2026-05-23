"""Schedule pipeline lag — time from compound enqueue → worker commit.

Replaces the SLO card on the observability page (which was teaching-level
demo content with limited operational value). Schedule lag is the
honest "is the worker keeping up?" signal:

* lag low + stable → pipeline healthy
* lag spikes → worker is backed up (queue draining slowly)
* lag flat-lines high → worker died with backlog

Storage follows the same Redis-sorted-set pattern as RED metrics
(``app/core/red_metrics.py``): every sample is a ``(timestamp_ms,
lag_ms)`` pair, the set is trimmed to the trailing 5 minutes on every
write. P50 / P95 / max are computed at read time, no precomputation.

Producer:
* ``schedule_queue.enqueue_compound`` stamps ``_enqueued_at_ms`` on the
  payload as it lands in Redis.
* ``compound_finalize.perform_compound_db_action`` reads it back on
  successful commit and calls :func:`record_lag_sample` with the
  observed delta.
"""

from __future__ import annotations

import json
import time
from functools import lru_cache
from typing import cast

import structlog
from redis import Redis

from app.core.config import get_settings
from app.schemas.system import ScheduleLagStats

logger = structlog.get_logger(__name__)

# Sorted set member shape: '{"ts":<ms>,"lag":<ms>}'. Member must be
# unique because ZSET deduplicates by member; including ts in the JSON
# (which is also the score) means rapid samples within the same
# millisecond would collide. Add a monotonic seq via INCR to guarantee
# uniqueness regardless of clock granularity.
_LAG_SET_KEY = "metrics:schedule_lag"
_LAG_SEQ_KEY = "metrics:schedule_lag:seq"

# Trim window — anything older than this gets dropped on each write.
# 1h covers the widest pill on the observability page (15m / 1h) so the
# UI never has to silently fall back to "5 min of data when you asked
# for 1h". Cost: ~2-3 MB of Redis at ~10 ops/sec sustained — fine.
_RETENTION_SECONDS = 3600


@lru_cache(maxsize=1)
def _redis() -> Redis:
    return Redis.from_url(
        str(get_settings().REDIS_URL),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def record_lag_sample(lag_ms: int) -> None:
    """Push one lag observation into the trailing-window Redis ZSET.

    Best-effort: failures are logged + swallowed. Missing samples just
    mean the percentiles are computed over a slightly smaller dataset.
    """
    try:
        rds = _redis()
        now_ms = int(time.time() * 1000)
        seq = cast("int", rds.incr(_LAG_SEQ_KEY))
        member = json.dumps({"ts": now_ms, "lag": lag_ms, "seq": seq})
        pipe = rds.pipeline(transaction=False)
        pipe.zadd(_LAG_SET_KEY, {member: now_ms})
        # Trim everything older than the retention window so the set
        # stays bounded regardless of traffic spikes.
        cutoff_ms = now_ms - _RETENTION_SECONDS * 1000
        pipe.zremrangebyscore(_LAG_SET_KEY, "-inf", cutoff_ms)
        pipe.execute()
    except Exception as exc:
        logger.warning("schedule_lag.record_failed", error=str(exc))


def compute_window(window_seconds: int) -> ScheduleLagStats:
    """Aggregate the trailing ``window_seconds`` of lag samples.

    Empty window → returns a zero envelope so the frontend can render a
    stable "no data yet" card instead of erroring.
    """
    try:
        rds = _redis()
        now_ms = int(time.time() * 1000)
        floor_ms = now_ms - window_seconds * 1000
        raws = cast(
            "list[str]",
            rds.zrangebyscore(_LAG_SET_KEY, floor_ms, "+inf"),
        )
    except Exception as exc:
        logger.warning("schedule_lag.read_failed", error=str(exc))
        return ScheduleLagStats(
            window_seconds=window_seconds,
            sample_count=0,
            p50_ms=0,
            p95_ms=0,
            max_ms=0,
        )

    lags: list[int] = []
    for raw in raws:
        try:
            doc = json.loads(raw)
            lags.append(int(doc["lag"]))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    if not lags:
        return ScheduleLagStats(
            window_seconds=window_seconds,
            sample_count=0,
            p50_ms=0,
            p95_ms=0,
            max_ms=0,
        )

    lags.sort()
    n = len(lags)

    def percentile(p: float) -> int:
        # Nearest-rank percentile (same convention as RED's percentiles).
        idx = max(0, min(n - 1, round(p * (n - 1))))
        return lags[idx]

    return ScheduleLagStats(
        window_seconds=window_seconds,
        sample_count=n,
        p50_ms=percentile(0.50),
        p95_ms=percentile(0.95),
        max_ms=lags[-1],
    )
