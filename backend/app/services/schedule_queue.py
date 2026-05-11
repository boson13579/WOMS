"""Redis I/O helper for pushing compounds onto the scheduler queue.

Lives separately from :mod:`app.services.scheduling` because that module is
intentionally pure (no Redis, no DB, no FastAPI). This module is the *thin*
layer that knows how to:

1. Allocate a sequence number (``INCR schedule:pending_ops:seq``).
2. Serialize a compound (:class:`ScheduleCompoundRequest`) into a sorted-set
   member and ``ZADD`` at the right score.
3. Maintain the ``schedule:pending_ops:by_compound_id`` secondary index so
   future cancel-by-compound-id lookups are O(1).
4. Conditionally fire ``run_scheduling_task.delay()`` when no worker is
   currently running.

API endpoints (``app.api.v1.schedule``) and Order CRUD services
(``app.services.order``) both call into here so neither has to know about
Redis key naming, score encoding, or Celery task dispatch.

The Celery task ``run_scheduling_task`` is dispatched via ``send_task`` by
name rather than direct import — that way this services module never needs
to import from ``workers``, avoiding the circular import that would
otherwise arise (``workers.scheduling`` already imports
``services.scheduling``).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, cast

import structlog
from redis import Redis

from app.core.config import get_settings
from app.schemas.schedule import ScheduleCompoundRequest
from app.services.scheduling import (
    PENDING_OPS_KEY,
    PENDING_OPS_SEQ_KEY,
    STATUS_KEY,
    score_for_op,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "BY_COMPOUND_ID_KEY",
    "enqueue_compound",
]


# Secondary index: compound_id → serialized sorted-set member. Allows the
# future ``DELETE /schedule/operations/{compound_id}`` cancellation endpoint
# (Phase 3) to look up the exact member string for ``ZREM`` in O(1) instead
# of scanning the whole sorted set.
BY_COMPOUND_ID_KEY = "schedule:pending_ops:by_compound_id"


@lru_cache(maxsize=1)
def _redis() -> Redis:
    """Module-level Redis client; instantiated on first use.

    Separate from ``workers.scheduling._get_redis`` and
    ``api.v1.schedule._redis`` so each layer can be unit-tested with its
    own monkeypatched fake. The ``lru_cache`` makes this a one-shot singleton
    inside the process; tests typically reach in via
    ``monkeypatch.setattr("app.services.schedule_queue._redis", ...)``.
    """
    return Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True)


def _read_status() -> dict[str, Any] | None:
    raw = cast("str | None", _redis().get(STATUS_KEY))
    if raw is None:
        return None
    return cast("dict[str, Any]", json.loads(raw))


def enqueue_compound(compound: ScheduleCompoundRequest) -> None:
    """Push a compound onto the pending_ops queue and trigger a run if idle.

    Steps (all atomic from the caller's perspective; no partial Redis state
    if any single primitive fails):

    1. ``INCR schedule:pending_ops:seq`` → seq number for ordering / member
       uniqueness.
    2. Serialize the compound into JSON with ``_seq`` embedded.
    3. ``ZADD schedule:pending_ops`` at ``score_for_op(group, seq)``.
    4. ``HSET schedule:pending_ops:by_compound_id`` for cancel-by-id lookups.
    5. If the worker is not currently running, ``run_scheduling_task.delay()``
       via ``send_task`` (avoids importing from workers/ to keep the layer
       direction clean).

    The endpoint / service caller passes a fully-validated
    :class:`ScheduleCompoundRequest` — this function does no business
    validation (group correctness, op ordering, etc.) beyond what the schema
    already enforces.
    """
    rds = _redis()
    seq = cast("int", rds.incr(PENDING_OPS_SEQ_KEY))

    payload = compound.model_dump(mode="json")
    payload["_seq"] = seq
    member = json.dumps(payload)

    score = score_for_op(group=compound.group, seq=seq)
    rds.zadd(PENDING_OPS_KEY, {member: score})
    rds.hset(BY_COMPOUND_ID_KEY, str(compound.compound_id), member)

    logger.info(
        "schedule.compound.enqueued",
        compound_id=str(compound.compound_id),
        group=compound.group,
        op_count=len(compound.ops),
        seq=seq,
    )

    status_doc = _read_status()
    if status_doc is None or status_doc.get("state") != "running":
        _send_run_task()


def _send_run_task() -> None:
    """Dispatch ``scheduling.run`` by task name (no workers/ import here).

    ``send_task`` lets us fire the task without importing the worker
    module — workers/ already imports services/, and a direct import the
    other way would close the cycle. Tests typically monkeypatch this
    function rather than the lazy ``celery_app`` import.
    """
    # Local import avoids a top-level circular dependency: workers/
    # imports services.scheduling, and the queue helper lives next to it.
    # ``celery_app`` itself doesn't depend on workers.scheduling, so this
    # is safe.
    from app.workers.celery_app import celery_app  # noqa: PLC0415 — see docstring.

    celery_app.send_task("scheduling.run")
