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
import uuid
from enum import StrEnum
from functools import lru_cache
from typing import Any, cast

import structlog
from redis import Redis

from app.core.config import get_settings
from app.schemas.schedule import ScheduleCompoundRequest
from app.services import websocket
from app.services.scheduling import (
    MATERIALIZE_NOTIFY_PENDING_KEY,
    PENDING_OPS_KEY,
    PENDING_OPS_SEQ_KEY,
    STATUS_KEY,
    score_for_op,
)


class CancelResult(StrEnum):
    """Outcome of a cancel-compound request.

    - ``cancelled`` — compound was in queue, ZREM removed it, WS notify fired.
    - ``in_progress`` — compound was found in the secondary index but
      ZREM returned 0 (worker just popped it; cancellation lost the race).
    - ``not_found`` — compound id is unknown to the queue (either it was
      never enqueued, or it was processed long enough ago that the index
      entry was cleaned).
    """

    cancelled = "cancelled"
    in_progress = "in_progress"
    not_found = "not_found"

logger = structlog.get_logger(__name__)

__all__ = [
    "BY_COMPOUND_ID_KEY",
    "CancelResult",
    "cancel_compound",
    "enqueue_compound",
    "enqueue_notify_user",
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


def cancel_compound(compound_id: uuid.UUID) -> CancelResult:
    """Try to remove a queued compound from ``schedule:pending_ops`` by id.

    Uses the ``schedule:pending_ops:by_compound_id`` secondary index
    maintained by ``enqueue_compound`` to look up the serialized member
    string in O(1), then ``ZREM`` it from the sorted set and ``HDEL`` the
    index entry. If ``ZREM`` returns 0 the compound was popped by the
    worker in the moment between our ``HGET`` and ``ZREM`` — we report
    ``in_progress`` so the caller can return 409, and we still ``HDEL``
    the index entry (worker's best-effort cleanup may already have
    fired, but defensive cleanup is cheap).

    On successful cancel, emits ``schedule.compound_cancelled`` to the
    compound's ``requested_by`` so the frontend can clear any optimistic
    UI state (e.g., re-enable a "Cancel" button or restore an indicator).
    """
    rds = _redis()
    compound_id_str = str(compound_id)

    member_raw = cast("str | None", rds.hget(BY_COMPOUND_ID_KEY, compound_id_str))
    if member_raw is None:
        logger.info(
            "schedule.compound.cancel_not_found",
            compound_id=compound_id_str,
        )
        return CancelResult.not_found

    removed = cast("int", rds.zrem(PENDING_OPS_KEY, member_raw))
    # Always clean up the index entry, regardless of zrem result — the
    # worker's best-effort cleanup may have already nuked the sorted set
    # entry but not the index.
    rds.hdel(BY_COMPOUND_ID_KEY, compound_id_str)

    if removed == 0:
        # Worker won the race: it popped the compound between our HGET
        # and ZREM. The compound is already being processed; cancellation
        # can no longer take effect.
        logger.info(
            "schedule.compound.cancel_race_lost",
            compound_id=compound_id_str,
        )
        return CancelResult.in_progress

    # Successfully removed from queue. Notify the requester.
    try:
        payload = json.loads(member_raw)
        requested_by_raw = payload.get("requested_by")
    except json.JSONDecodeError:
        requested_by_raw = None
    if requested_by_raw:
        websocket.notify_user(
            user_id=uuid.UUID(requested_by_raw),
            message={
                "type": "schedule.compound_cancelled",
                "compound_id": compound_id_str,
            },
        )

    logger.info(
        "schedule.compound.cancelled",
        compound_id=compound_id_str,
    )
    return CancelResult.cancelled


def enqueue_notify_user(user_id: uuid.UUID) -> None:
    """Mark *user_id* as awaiting a materializer notification.

    Called by ``run_scheduling_task`` after a compound succeeds. The
    materializer drains this set (via atomic RENAME swap) and emits
    ``schedule.materialized`` to each user once DB rows reflecting the
    in-memory state have been written. Users whose compounds arrive
    *after* the materializer's drain go into the next batch; they're not
    silently dropped.

    Set semantics naturally dedupe: multiple compounds from the same user
    in one materializer window collapse to one notification.
    """
    _redis().sadd(MATERIALIZE_NOTIFY_PENDING_KEY, str(user_id))


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
