"""Celery scheduling tasks.

Wraps the pure scheduler in :mod:`app.services.scheduling` with the side-effects
needed for production:

- Persists ``SchedulerState`` to Redis between runs (key ``schedule:state``).
- Drains a Redis **sorted set** of pending order ops (``schedule:pending_ops``;
  producers ``ZADD`` with a score that encodes ``shrink-before-grow`` plus
  FIFO-within-group, and we ``ZPOPMIN`` — both are O(log n)). The score is
  built from a monotonic ``INCR`` counter at ``schedule:pending_ops:seq``.
- Tracks task lifecycle in ``schedule:status`` for dashboards / advance_day.
- Writes computed schedule rows back into Postgres.
- Notifies clients via the ``app.services.websocket`` placeholder.

Three tasks live here:

- ``run_scheduling_task`` — drain pending ops, mutate state, persist, notify,
  and re-fire itself if more ops queued during the run.
- ``advance_day_task`` — daily 00:00 UTC tick; waits for any in-flight run,
  rolls the horizon forward by a day, then re-triggers scheduling.
- ``rebuild_schedule_task`` — fired by ``POST /schedule/rebuild``; waits for
  any in-flight run, rebuilds state from DB, notifies skipped orders'
  creators, then re-triggers scheduling.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any, cast

import structlog
from celery import Task
from redis import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logger import audit_log as emit_audit_log
from app.models.order import Order, OrderStatus
from app.repositories import audit_log as audit_log_repo
from app.repositories import order as order_repo
from app.services import order as order_service
from app.services import websocket
from app.services.schedule_queue import enqueue_notify_user
from app.services.scheduling import (
    MATERIALIZE_NOTIFY_PENDING_KEY,
    MATERIALIZE_NOTIFY_PROCESSING_KEY,
    MATERIALIZE_RUNNING_KEY,
    PENDING_OPS_KEY,
    STATE_KEY,
    STATUS_KEY,
    BatchOp,
    SchedulerState,
    SchedulingOrder,
    advance_day,
    apply_batch_to_capacity,
    apply_batch_to_deadline,
    compute_batch_capacity_delta,
    compute_schedule,
    is_batch_feasible,
    pin_order,
    pq_add,
    pq_remove,
    rebuild_state,
    unpin_order,
)
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

__all__ = ["advance_day_task", "rebuild_schedule_task", "run_scheduling_task"]


# ---------------------------------------------------------------------------
# Status / waiter-flag constants (worker-internal)
# ---------------------------------------------------------------------------
#
# The cross-layer contract keys (``STATE_KEY`` / ``STATUS_KEY`` /
# ``PENDING_OPS_KEY`` / ``PENDING_OPS_SEQ_KEY``) and ``score_for_op`` live in
# ``app.services.scheduling`` — see the import block above. The waiter flag
# is purely an internal coordination knob between the three worker tasks and
# is not part of the API contract, so it stays here.

WAITER_FLAG_KEY = "schedule:waiter_pending"

# P0-2/P0-3: hard mutex around any write to ``schedule:state``.
#
# Pre-existing coordination (``schedule:status`` polling +
# ``schedule:waiter_pending`` flag) is *advisory* — two ``run_scheduling_task``
# invocations can both observe ``status='idle'`` and both proceed to write
# state, losing one compound's effect when their saves race. The status key
# is great for "is something happening?" observability but it is not an
# atomic test-and-set, so under prod conditions (``--concurrency >= 2`` or
# multiple worker containers) the gap between ``_set_status(running)`` and
# the next ``_save_state`` is enough for a second task to slip in.
#
# This lock closes that gap: each state-writing task ``SET NX EX`` s its
# task_id as the value and holds it for its whole body. ``run_scheduling_task``
# returns early on contention (the holder will pick up the queued compound
# on its self-retrigger). ``advance_day_task`` / ``rebuild_schedule_task``
# poll for the lock for a bounded window (they MUST run, can't skip).
#
# TTL is the same 5-min safety window we use elsewhere — long enough that
# no honest task body exceeds it, short enough that a crashed task's
# orphaned lock is recovered within an operator-visible timeframe.
# Dead-letter list for ``pending_ops`` members that fail to JSON-decode.
# Pre-P1-5 a malformed member would be silently discarded after ZPOPMIN,
# leaving the affected order's ``is_processing_locked=True`` row stuck
# forever (no way to recover ``requested_by`` for a compound_failed
# notify, no way to know which order_id to unlock). The DLQ retains the
# raw bytes so ops can decode them out-of-band (manual inspection,
# scripts, sentry-style alerting on key existence) and unlock affected
# orders by hand. ``RPUSH`` semantics: oldest at the head, newest at
# the tail — preserves the order of arrival for forensic analysis.
PENDING_OPS_DLQ_KEY = "schedule:pending_ops:dlq"

STATE_WRITER_LOCK_KEY = "schedule:state_writer_lock"
_STATE_WRITER_LOCK_TTL_SECONDS = 300
# Lua compare-and-delete: only release the lock if its value still matches
# our task_id. Protects against the case where our TTL expired, another task
# acquired it, and our finally block then naively ``DEL``s — releasing
# someone else's lock would re-open the race we're trying to close.
_STATE_WRITER_LOCK_CAS_DELETE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) "
    "else return 0 end"
)

_STATUS_IDLE = "idle"
_STATUS_RUNNING = "running"
_STATUS_FAILED = "failed"

# Crash-safety TTL for the waiter flag. If a waiter (advance_day_task /
# rebuild_schedule_task) sets the flag and then dies before its finally
# clears it, this TTL ensures future run_scheduling_task invocations stop
# yielding to a phantom waiter. 10 minutes is well above the worst-case
# wall time of any waiter body (5 min wait + work) but short enough that a
# real crash recovers within an operator-visible timeframe. Tunable via
# ``SCHEDULER_WAITER_FLAG_TTL_SECONDS`` env var.
_WAITER_FLAG_TTL_SECONDS = get_settings().SCHEDULER_WAITER_FLAG_TTL_SECONDS

# advance_day_task and rebuild_schedule_task both wait at most this long for
# an in-flight run to finish before proceeding. Polling cadence is short
# enough to be responsive and long enough to keep Redis traffic negligible.
# Tunable via ``SCHEDULER_RUN_WAIT_TIMEOUT_SECONDS`` /
# ``SCHEDULER_RUN_WAIT_POLL_INTERVAL_SECONDS`` env vars.
_RUN_WAIT_TIMEOUT_SECONDS = get_settings().SCHEDULER_RUN_WAIT_TIMEOUT_SECONDS
_RUN_WAIT_POLL_INTERVAL_SECONDS = get_settings().SCHEDULER_RUN_WAIT_POLL_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# Reject-rate adaptive cap for batch admission
# ---------------------------------------------------------------------------
#
# ``_largest_halving_feasible_prefix`` halves from ``len(candidates)`` down to
# the first feasible attempt. When the queue holds N=1000 compounds but the
# very first compound is the only blocker, halving from 1000 wastes
# ``log2(N)`` rounds of ``compute_batch_capacity_delta`` (each O(K+D)) on
# prefixes that were doomed before we built their delta. The "look at the
# first 1/p compounds" heuristic caps the candidate window at the EXPECTED
# position of the next reject — so with p=0.01 (1 in 100 compounds rejected)
# we never test prefixes larger than 100.
#
# p is updated per-compound via EWMA (alpha = 0.05 → roughly 20 events to fully
# adapt) and persisted to Redis so the rate survives worker restarts and is
# shared across worker replicas (last-writer-wins on concurrent updates;
# acceptable noise for a tuning heuristic).
COMPOUND_REJECT_RATE_KEY = "schedule:compound_reject_rate"
_REJECT_RATE_INITIAL = 0.01
# Floor: a worker that's seen only accepts would otherwise let p → 0 and
# take = N (= no cap, same as pre-heuristic behavior). 1e-4 caps take at
# 10_000 — well above any sane pending-queue depth, so still effectively
# uncapped but bounded.
_REJECT_RATE_MIN = 1e-4
# Ceiling: prevents p > 1 from a buggy update path (would imply take < 1).
_REJECT_RATE_MAX = 1.0
# Smoothing factor. alpha small ⇒ p adapts slowly to recent events; alpha large ⇒
# p over-reacts to one-off rejections. 0.05 gives ~20 events for full
# adaptation, which is roughly the resolution we want (a sustained shift
# in reject pattern is noticed within a couple of typical batches; an
# isolated reject barely moves p).
_REJECT_RATE_ALPHA = 0.05


def _get_reject_rate() -> float:
    """Read the EWMA reject rate from Redis; fall back to the initial prior.

    Clamps to ``[MIN, MAX]`` defensively in case a corrupted value got into
    the key (manual surgery, future schema change, etc.) — a wild p would
    poison the take-count computation otherwise.
    """
    raw = cast("str | None", _get_redis().get(COMPOUND_REJECT_RATE_KEY))
    if raw is None:
        return _REJECT_RATE_INITIAL
    try:
        rate = float(raw)
    except (ValueError, TypeError):
        logger.warning("schedule.reject_rate.corrupted_value", raw=raw)
        return _REJECT_RATE_INITIAL
    return max(_REJECT_RATE_MIN, min(_REJECT_RATE_MAX, rate))


def _update_reject_rate(*, accepted: int, rejected: int) -> None:
    """Apply per-compound EWMA updates to the persisted reject rate.

    Each accepted compound pulls p toward 0; each rejected compound pulls
    p toward 1. We apply the updates sequentially (one EWMA step per
    compound) rather than collapsing the batch into a single ratio — so
    within a single call, the explicit order is **all accepts first, then
    all rejects**. Different per-call shapes (one ``(a=N, r=M)`` vs two
    consecutive ``(a=N, r=0)`` + ``(a=0, r=M)``) produce the SAME final p
    because the inter-call concatenation preserves that accept-then-reject
    ordering.

    Note EWMA is NOT commutative across orderings in general — e.g.
    ``(a=1)`` then ``(r=1)`` lands at a different p than ``(r=1)`` then
    ``(a=1)``. Callers that care about exact final value must batch
    observations in a single call; callers that just need "monotonic
    drift in the right direction" can fire updates ad-hoc.

    Concurrent writes (multiple workers) race on the SET — last-writer-wins.
    Acceptable for a tuning heuristic: convergence is slower but still
    monotonic in the expected direction.
    """
    if accepted == 0 and rejected == 0:
        return
    rate = _get_reject_rate()
    # Each accepted observation: target = 0
    for _ in range(accepted):
        rate = (1 - _REJECT_RATE_ALPHA) * rate
    # Each rejected observation: target = 1
    for _ in range(rejected):
        rate = _REJECT_RATE_ALPHA + (1 - _REJECT_RATE_ALPHA) * rate
    rate = max(_REJECT_RATE_MIN, min(_REJECT_RATE_MAX, rate))
    _get_redis().set(COMPOUND_REJECT_RATE_KEY, repr(rate))


def _take_count_from_rate(pending_count: int, rate: float) -> int:
    """How many leading pending compounds to consider in this iteration.

    ``take = min(pending_count, ceil(1/p))``. Floored at 1 so we always
    look at at least the head of the queue (otherwise an unstable p that
    momentarily exceeded 1 would freeze the drain).
    """
    cap = max(1, math.ceil(1.0 / rate))
    return min(pending_count, cap)


# ---------------------------------------------------------------------------
# Lazy Redis client
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_redis() -> Redis:
    """Module-level Redis client; instantiated on first use."""
    return Redis.from_url(str(get_settings().REDIS_URL), decode_responses=True)


# ---------------------------------------------------------------------------
# State / status / pending_ops accessors
# ---------------------------------------------------------------------------


def _load_state() -> SchedulerState:
    """Read ``schedule:state`` or initialize a fresh one anchored at today."""
    raw = cast("str | None", _get_redis().get(STATE_KEY))
    if raw is None:
        return SchedulerState.initial(datetime.now(tz=UTC).date())
    return SchedulerState.from_json(raw)


def _save_state(state: SchedulerState) -> None:
    """Serialize and persist the scheduler state to Redis."""
    _get_redis().set(STATE_KEY, state.to_json())


def _set_status(
    *,
    state: str,
    started_at: str | None = None,
    finished_at: str | None = None,
    task_id: str | None = None,
    error: str | None = None,
) -> None:
    """Overwrite ``schedule:status`` with a single JSON document."""
    payload = {
        "state": state,
        "started_at": started_at,
        "finished_at": finished_at,
        "task_id": task_id,
        "error": error,
    }
    _get_redis().set(STATUS_KEY, json.dumps(payload))


def _get_status() -> dict[str, Any] | None:
    raw = cast("str | None", _get_redis().get(STATUS_KEY))
    if raw is None:
        return None
    return cast("dict[str, Any]", json.loads(raw))


# ---------------------------------------------------------------------------
# State-writer lock (P0-2 / P0-3)
# ---------------------------------------------------------------------------


def _try_acquire_state_lock(task_id: str) -> bool:
    """Try to claim the state-writer lock; return whether we got it.

    ``SET NX EX`` is the atomic primitive we need: only one client wins
    when many concurrently try the same key. Stamping the value with
    ``task_id`` lets the release path verify ownership before deleting
    (see ``_release_state_lock``).
    """
    return bool(
        _get_redis().set(
            STATE_WRITER_LOCK_KEY,
            task_id,
            nx=True,
            ex=_STATE_WRITER_LOCK_TTL_SECONDS,
        )
    )


def _acquire_state_lock_blocking(task_id: str, timeout_seconds: float) -> bool:
    """Poll for the state-writer lock until acquired or timeout elapses.

    Used by ``advance_day_task`` / ``rebuild_schedule_task`` — they
    can't just skip on contention because they're scheduled / on-demand,
    not opportunistic. Polling at the same cadence as
    ``_wait_for_idle_run`` keeps Redis traffic negligible.
    """
    poll = _RUN_WAIT_POLL_INTERVAL_SECONDS
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _try_acquire_state_lock(task_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def _release_state_lock(task_id: str) -> None:
    """Best-effort release with compare-and-delete.

    If we held the lock straight through, this just DELs it. If our TTL
    expired and somebody else took it, the CAS no-ops — we never blow
    away another task's lock.
    """
    try:
        _get_redis().eval(
            _STATE_WRITER_LOCK_CAS_DELETE,
            1,
            STATE_WRITER_LOCK_KEY,
            task_id,
        )
    except Exception as exc:
        logger.warning(
            "schedule.state_lock.release_failed",
            task_id=task_id,
            error=str(exc),
        )


def _set_waiter_flag() -> None:
    """Mark that a waiter task is in the pipeline.

    A ``run_scheduling_task`` that finishes while this flag is set must NOT
    re-trigger itself even if more ops are queued — the waiter
    (``advance_day_task`` / ``rebuild_schedule_task``) takes ownership of
    the next ``run_scheduling_task.delay()`` after its own work finishes.
    Without this flag the re-triggered run_task and the waiter race each
    other writing ``schedule:state``: the waiter sees ``status=idle`` the
    instant the run_task flips it, but run_task hasn't yet fired its
    re-trigger, and ends up doing so a few microseconds later → both run
    concurrently.

    A 10-minute TTL guards against a crashed waiter permanently
    suppressing re-triggers; after expiry the system is back to the
    "no waiter" baseline.
    """
    _get_redis().set(WAITER_FLAG_KEY, "1", ex=_WAITER_FLAG_TTL_SECONDS)


def _clear_waiter_flag() -> None:
    """Drop the waiter flag.

    Called from a ``finally`` so it always runs, even when the waiter body
    raised.
    """
    _get_redis().delete(WAITER_FLAG_KEY)


def _is_waiter_pending() -> bool:
    """True iff a waiter has set the flag and not yet cleared it."""
    return _get_redis().get(WAITER_FLAG_KEY) is not None


def _read_pending_compounds(*, limit: int | None = None) -> list[tuple[str, dict[str, Any]]]:
    """Read every pending compound in priority order WITHOUT popping.

    Returns ``[(raw_member, parsed_dict), ...]`` sorted ascending by
    sorted-set score (= shrink-group first, then grow; FIFO within each
    group). The raw member string is preserved so the caller can
    ``ZREM`` exactly that member after batch acceptance / single-
    compound rejection — necessary because ``ZRANGE`` only reads.

    Why not ``ZPOPMIN``: the batch admission path needs to inspect a
    candidate prefix and possibly reject it (binary-search halving) WITHOUT
    losing the compounds we decided not to accept yet. Popping each
    compound, deciding feasibility, then re-adding rejected ones would
    burn O(N) ZADD ops per round and would also reset FIFO ordering for
    re-added members (their score reflects original seq, not re-add seq —
    but ``ZADD`` semantics on same member overwrite, so the queue position
    is preserved; the real cost is the unnecessary round-trips). Read-
    only inspection + targeted ``ZREM`` post-commit is the cleaner shape.

    Malformed JSON entries are drained to ``PENDING_OPS_DLQ_KEY`` and
    ``ZREM``'d here so the batch path's binary search doesn't have to
    skip them mid-decision. Same forensic-preservation rationale as the
    old ``_pop_next_compound``: ZADD already removed the member's
    placement information when we ZREM it here, but the raw bytes go
    onto a List so ops can recover the affected ``order_id`` /
    ``requested_by`` manually.

    ``limit`` (optional): cap the ZRANGE read at the first ``limit``
    members. The drain loop only ever inspects ``pending[:take]`` where
    ``take ≤ ceil(1/p)``, so reading the whole queue is wasted O(N)
    bandwidth per drain iteration (and O(N²) across a long drain). Set
    ``limit`` to the reject-rate cap to bound traffic to what we'll
    actually use; ``None`` (= legacy) reads everything. Any entries past
    ``limit`` stay in pending and get picked up on the next iteration.
    A DLQ drain mid-window can reduce the returned count below
    ``limit`` — that's OK, the next iteration re-reads.
    """
    rds = _get_redis()
    end_idx = -1 if limit is None else max(0, limit - 1)
    members = cast(
        "list[tuple[str, float]]",
        rds.zrange(PENDING_OPS_KEY, 0, end_idx, withscores=True),
    )
    parsed: list[tuple[str, dict[str, Any]]] = []
    for member, _score in members:
        try:
            payload = cast("dict[str, Any]", json.loads(member))
        except json.JSONDecodeError:
            # Drain to DLQ + ZREM the malformed entry so it doesn't
            # block batch admission on the next read.
            _drain_corrupted_to_dlq(
                rds,
                member,
                reason="malformed_json",
                score=_score,
            )
            continue
        # Defensive: payload self-declares its op count. A mismatch means the
        # member was tampered with post-enqueue (manual surgery, partial
        # write, bit flip) — we have no idea what's actually in the ops list,
        # so drain it to DLQ rather than admit a half-truncated business
        # action. Schema validation at producer time already enforces the
        # count, so this branch should never fire in practice.
        declared_op_count = payload.get("op_count")
        actual_ops = payload.get("ops") or []
        if declared_op_count is not None and declared_op_count != len(actual_ops):
            _drain_corrupted_to_dlq(
                rds,
                member,
                reason="op_count_mismatch",
                score=_score,
                declared=declared_op_count,
                actual=len(actual_ops),
            )
            continue
        parsed.append((member, payload))
    return parsed


def _drain_corrupted_to_dlq(
    rds: Redis,
    member: str,
    *,
    reason: str,
    score: float,
    **extras: Any,
) -> None:
    """ZREM a corrupted pending member + RPUSH onto DLQ + ERROR log.

    Shared between the JSON-decode failure path and the op_count-mismatch
    guard inside ``_read_pending_compounds``. Catches Redis errors during
    the drain itself so we don't crash the whole task on a transient
    network blip — the corrupted member stays in pending and gets retried
    on the next read; the ERROR log surfaces the situation to ops.
    """
    try:
        rds.zrem(PENDING_OPS_KEY, member)
        rds.rpush(PENDING_OPS_DLQ_KEY, member)
    except Exception as exc:
        logger.error(
            "schedule.pending_compound.dlq_drain_failed",
            raw=member,
            reason=reason,
            error=str(exc),
        )
        return
    logger.error(
        "schedule.pending_compound.corrupted_drained_to_dlq",
        raw=member,
        reason=reason,
        score=score,
        dlq_key=PENDING_OPS_DLQ_KEY,
        **extras,
    )


def _op_to_scheduling_order(op: dict[str, Any]) -> SchedulingOrder:
    """Translate a queued op dict into the pure scheduler's input schema."""
    return SchedulingOrder(
        order_id=uuid.UUID(op["order_id"]),
        order_number=op["order_number"],
        wafer_quantity=int(op["wafer_quantity"]),
        deadline=date.fromisoformat(op["deadline"]),
    )


# ---------------------------------------------------------------------------
# Batch admission — read pending → binary search feasible prefix → commit
# ---------------------------------------------------------------------------
#
# Pre-rewrite: ``run_scheduling_task`` popped ONE compound, took a
# ``SchedulerState`` snapshot, applied each leaf op via single-order
# ``add_order`` / ``remove_order`` (with O(D log D) tree backward-fill per
# op), and rolled back the snapshot on any per-op failure (saga). For a
# burst of N compounds that's N task invocations + N snapshots + N*K
# per-op tree operations.
#
# This rewrite reads the WHOLE pending queue in priority order, binary-
# searches the largest feasible prefix ``[1..k]`` of compounds (halving
# from N until the first prefix passes ``is_batch_feasible``), then
# applies the whole batch in a single pair of tree updates (``apply_batch_
# to_capacity`` + ``apply_batch_to_deadline``) followed by per-compound
# pq + pin/unpin + DB + WebSocket. After a batch commits, the loop re-
# reads pending (which may have grown via concurrent enqueues) and
# searches again. First-compound infeasibility (``k=0``) drops only that
# compound and notifies ``compound_failed``.
#
# Saga rollback is intentionally gone: ``is_batch_feasible`` is checked
# BEFORE any tree mutation, so an accepted batch never partially fails.
# Pin/unpin's ``capacity_exceeded`` branch is treated as a producer-side
# admission bug (defensive warning log), not as a compound failure.


def _extract_batch_ops(compounds: list[dict[str, Any]]) -> list[BatchOp]:
    """Project add/remove leaf ops across a batch of compounds into ``BatchOp``s.

    Pin/unpin leaf ops are intentionally skipped — by design they do not
    contribute to the per-day capacity delta table (producer-side admission
    control vets fake_deadline feasibility before enqueueing). Their
    capacity-tree swap (real→fake or fake→real) is applied per-compound
    inside ``_apply_compound_leaf_structural`` via the existing
    ``pin_order`` / ``unpin_order`` helpers, which are self-contained.
    """
    ops: list[BatchOp] = []
    for compound in compounds:
        for leaf in compound.get("ops", []):
            kind = leaf.get("op")
            if kind not in ("add", "remove"):
                continue
            ops.append(
                BatchOp(
                    kind=kind,
                    wafer_quantity=int(leaf["wafer_quantity"]),
                    deadline=date.fromisoformat(leaf["deadline"]),
                )
            )
    return ops


def _largest_halving_feasible_prefix(
    state: SchedulerState,
    compounds: list[dict[str, Any]],
) -> tuple[int, int]:
    """Halving probe for the largest feasible compound prefix.

    Sequence (per user spec, ``N = len(compounds)``):

      attempt = N, N//2, N//4, ..., 1, then exit ⇒ 0

    The first attempt size whose ``[1..attempt]`` projection passes
    ``is_batch_feasible`` is returned. This is NOT a true binary search
    for the MAXIMUM feasible prefix — for ``N=3`` with feasibility
    pattern ``[1..1] OK, [1..2] OK, [1..3] FAIL`` we test ``[1..3]``
    (fail) then ``[1..1]`` (pass) and accept only 1 compound, even
    though 2 would have fit. The remaining compound(s) are picked up
    on the next outer-loop iteration (with possibly new arrivals).

    True binary search would find the optimal k in the same O(log N)
    probes but requires prefix-feasibility monotonicity, which mixed-sign
    grow-group compounds (qty-smaller + deadline-earlier) can break.
    Halving stays safe under broken monotonicity at the cost of accepting
    a possibly-suboptimal k.

    Returns ``(k, attempts_tried)``:
      - ``k = 0`` when even ``[1..1]`` is infeasible — caller treats
        that as "drop and reject the first compound", the only way out
        of a queue head that cannot fit.
      - ``attempts_tried`` is the number of halving rounds executed;
        ``attempts_tried - k`` is the count of probes that failed before
        the successful one (or all of them, when ``k == 0``). The reject-
        rate EWMA uses ``attempts_tried - k`` as a "this many prefix
        sizes were rejected" signal to bias the next iteration's cap.

    Complexity: O(log N · (K + D)) where K is total ops in the attempted
    prefix and D is HORIZON_DAYS. Each halving rebuilds the delta from
    scratch — could be incrementalized (re-use prefix delta and subtract
    the dropped tail) but the constant factor is small and halving
    converges quickly.
    """
    base_date = state.base_date
    attempt = len(compounds)
    attempts_tried = 0
    while attempt > 0:
        prefix = compounds[:attempt]
        batch_ops = _extract_batch_ops(prefix)
        delta = compute_batch_capacity_delta(batch_ops, base_date)
        attempts_tried += 1
        if is_batch_feasible(state, delta):
            return attempt, attempts_tried
        attempt //= 2
    return 0, attempts_tried


def _apply_compound_leaf_structural(
    state: SchedulerState,
    leaf: dict[str, Any],
) -> None:
    """Mutate ``pq`` / ``pinned_orders`` for one leaf op, post-batch-tree-update.

    Contract: caller MUST have already applied ``apply_batch_to_capacity``
    + ``apply_batch_to_deadline`` for the batch containing this leaf —
    so for ``add`` / ``remove`` the tree side is already done and this
    function only touches pq. For ``pin`` / ``unpin`` the trees still
    need a swap (the batch delta excludes pin/unpin by design); the
    existing ``pin_order`` / ``unpin_order`` do their own self-contained
    swap so we just delegate.

    Failure handling: ``pin`` / ``unpin`` failures (``deadline_too_far``
    / ``capacity_exceeded``) should not happen if the producer's
    admission control is correct. We log a warning rather than raise
    because (a) the batch tree updates have already committed and we
    can't easily roll them back, and (b) the worst case is one pin
    didn't take effect — the order is still in pq at real deadline,
    materializer will write DB consistently with that, and ops can
    notice via the warning log.
    """
    kind = leaf.get("op")
    if kind == "add":
        pq_add(state, _op_to_scheduling_order(leaf))
        return
    if kind == "remove":
        pq_remove(state, uuid.UUID(leaf["order_id"]))
        return
    if kind == "pin":
        fake_raw = leaf.get("fake_deadline")
        if fake_raw is None:
            logger.error(
                "schedule.batch.pin_missing_fake_deadline",
                order_id=leaf.get("order_id"),
            )
            return
        result = pin_order(
            state,
            _op_to_scheduling_order(leaf),
            date.fromisoformat(fake_raw),
        )
        if result.status != "success":
            logger.warning(
                "schedule.batch.pin_unexpected_failure",
                order_id=leaf.get("order_id"),
                status=result.status,
                message=result.message,
            )
        return
    if kind == "unpin":
        result = unpin_order(state, uuid.UUID(leaf["order_id"]))
        if result.status != "success":
            logger.warning(
                "schedule.batch.unpin_unexpected_failure",
                order_id=leaf.get("order_id"),
                status=result.status,
                message=result.message,
            )
        return
    logger.warning("schedule.batch.unknown_op", op=kind)


def _notify_compound_accepted(compound: dict[str, Any]) -> None:
    """WS notify + materializer notify-pending SADD for one accepted compound.

    Same envelope shape as the old per-compound success path so frontend
    contracts are preserved (``schedule.compound_accepted``). The SADD
    into ``materialize_notify_pending`` is what makes the slow-path
    materializer eventually emit ``schedule.materialized`` once DB rows
    catch up.
    """
    requested_by = compound.get("requested_by")
    if not requested_by:
        return
    user_id = uuid.UUID(requested_by)
    websocket.notify_user(
        user_id=user_id,
        message={
            "type": "schedule.compound_accepted",
            "compound_id": str(compound.get("compound_id")),
        },
    )
    enqueue_notify_user(user_id)


def _notify_compound_batch_rejected(compound: dict[str, Any]) -> None:
    """WS notify a compound that was rejected by single-compound infeasibility.

    Fired when ``_largest_halving_feasible_prefix`` returns 0 — even the
    first compound alone cannot fit current capacity. Envelope mirrors
    the pre-rewrite ``_notify_compound_failure`` so frontends see the
    same ``schedule.compound_failed`` shape; ``failed_op_index=0`` and
    ``rolled_back=True`` because no part of the compound was applied.
    """
    ops = compound.get("ops") or []
    first_op = ops[0] if ops else {}
    logger.warning(
        "schedule.compound.batch_rejected",
        compound_id=compound.get("compound_id"),
        first_op=first_op.get("op"),
    )
    requested_by = compound.get("requested_by")
    if not requested_by:
        return
    order_id_raw = first_op.get("order_id")
    websocket.notify_user(
        user_id=uuid.UUID(requested_by),
        message={
            "type": "schedule.compound_failed",
            "compound_id": str(compound.get("compound_id")),
            "failed_op_index": 0,
            "failed_op": first_op.get("op"),
            "order_id": str(order_id_raw) if order_id_raw else None,
            "order_number": first_op.get("order_number"),
            "reason": "capacity_exceeded",
            "detail": "Batch admission could not fit this compound.",
            "rolled_back": True,
        },
    )


def _commit_accepted_batch(
    state: SchedulerState,
    accepted: list[tuple[str, dict[str, Any]]],
) -> None:
    """Commit a feasible batch end-to-end: trees → pq → ZREM → DB → WS.

    Pipeline:

    1. Build the batch delta from add/remove ops in all accepted compounds
       and apply it ONCE to both segment trees (``apply_batch_to_capacity``
       with carry-back distribution, ``apply_batch_to_deadline`` direct).
       This is the single optimization that makes batched admission cheaper
       than per-compound: tree mutations collapse from K per batch to 2
       per batch.
    2. Per-compound, leaf-by-leaf: ``_apply_compound_leaf_structural``
       updates pq / pinned_orders only (or delegates to pin_order /
       unpin_order for swaps).
    3. ``ZREM`` all accepted members from the pending queue (one round-trip).
    4. ``_save_state`` once for the whole batch.
    5. Per-compound: drop secondary-index entry, run db_action, notify_user.

    State save is deliberately AFTER tree+pq commit but BEFORE per-
    compound DB / WS so a crash between save and DB leaves the queue
    drained and state advanced (no double-apply on restart) — at worst
    a few users miss the immediate WS ack and pick it up on the next
    state refresh.
    """
    rds = _get_redis()

    batch_ops = _extract_batch_ops([compound for _, compound in accepted])
    delta = compute_batch_capacity_delta(batch_ops, state.base_date)
    apply_batch_to_capacity(state, delta)
    apply_batch_to_deadline(state, delta)

    for _, compound in accepted:
        for leaf in compound.get("ops", []):
            _apply_compound_leaf_structural(state, leaf)

    members = [member for member, _ in accepted]
    if members:
        rds.zrem(PENDING_OPS_KEY, *members)

    _save_state(state)

    for _, compound in accepted:
        _drop_compound_index_entry(compound.get("compound_id"))
        _perform_compound_db_action(compound, accepted=True)
        _notify_compound_accepted(compound)

    logger.info(
        "schedule.batch.committed",
        compound_count=len(accepted),
        total_ops=sum(len(c.get("ops", [])) for _, c in accepted),
    )


def _reject_first_compound(member: str, compound: dict[str, Any]) -> None:
    """Drop the head compound when even ``[1..1]`` is infeasible.

    No tree / pq mutation: the binary search detected infeasibility
    BEFORE any state was touched, so rejection is a pure
    queue-and-compensate operation:

    1. ``ZREM`` the member from pending_ops.
    2. Drop the secondary-index entry (best-effort).
    3. Run ``_perform_compound_db_action(accepted=False)`` so the
       producer's ``is_processing_locked=True`` flag is cleared and any
       pre-created orphan rows are soft-deleted.
    4. WS-notify ``compound_failed`` to the requester.
    """
    rds = _get_redis()
    rds.zrem(PENDING_OPS_KEY, member)
    _drop_compound_index_entry(compound.get("compound_id"))
    _perform_compound_db_action(compound, accepted=False)
    _notify_compound_batch_rejected(compound)


def _drop_compound_index_entry(compound_id: str | None) -> None:
    """Best-effort cleanup of the by-compound-id secondary index.

    The index is maintained by ``schedule_queue.enqueue_compound`` so a
    future cancel-by-compound-id endpoint can ``ZREM`` in O(1). Once we
    ``ZPOPMIN`` a compound, the cancellation window is closed and the
    index entry is stale — best-effort remove keeps Redis from growing
    unboundedly.

    Catches only ``RedisError`` (P3-1): pre-fix this swallowed any
    ``Exception``, which would hide programming bugs (a TypeError from
    a refactor would silently go to a warning log instead of crashing
    the task and surfacing the bug in CI). The only thing we genuinely
    want to tolerate here is a transient Redis outage / timeout, all of
    which derive from ``RedisError``.
    """
    if not compound_id:
        return
    try:
        _get_redis().hdel("schedule:pending_ops:by_compound_id", compound_id)
    except RedisError as exc:
        # Logging only; a stale index entry doesn't break correctness.
        logger.warning(
            "schedule.compound.index_cleanup_failed",
            compound_id=compound_id,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Compound db_action — write user-facing columns on accept, compensate on reject
# ---------------------------------------------------------------------------
#
# P1-2 (PR-14 review): producers (``create_order`` / ``update_order`` /
# ``delete_order`` / ``batch_update_orders``) no longer commit the actual
# user-facing column writes (new ``wafer_quantity`` /
# ``requested_delivery_date`` / ``notes`` / ``is_deleted`` / etc.). They
# only write the ``is_processing_locked=True`` flag and embed a
# ``CompoundDbAction`` in the compound. The worker — which is the single
# authority on whether the in-memory ``SchedulerState`` accepted the
# change — executes the matching DB write here.
#
# Why: pre-P1-2 a rejected compound (capacity_exceeded / deadline_too_far)
# would leave DB with new values while state had rolled back to the old —
# permanently divergent until manual rebuild. With this handler the DB
# write only happens when state has actually accepted; on reject the
# producer's lock is cleared and DB columns remain at their pre-PATCH
# values.


def _perform_compound_db_action(
    compound: dict[str, Any],
    *,
    accepted: bool,
) -> None:
    """Execute the compound's ``db_action`` after state has settled.

    Idempotent at the row level: if the order disappeared between
    enqueue and execution (e.g. a follow-up DELETE landed first), this
    function logs a warning and returns without raising.

    Branch matrix:

    ``kind="create"``:
      - accepted: no-op (producer pre-created the row; materializer
        will fill scheduling cols).
      - rejected: orphan cleanup — set ``is_deleted=True`` and
        ``status=cancelled`` so the row exits user-visible queries.

    ``kind="update"``:
      - accepted: write the new ``wafer_quantity`` /
        ``requested_delivery_date`` / ``notes`` / ``assigned_to`` from
        ``db_action.new_*``; clear lock; emit ``order.updated`` audit
        record with the diff.
      - rejected: clear lock; restore ``status`` to ``scheduled`` if
        the row has a ``scheduled_production_date`` (which means it
        was already accepted into the schedule before this PATCH), or
        ``pending`` otherwise. DB columns themselves stay at pre-PATCH
        values because the producer never wrote them.

    ``kind="delete"``:
      - accepted: soft-delete (``is_deleted=True``,
        ``status=cancelled``); clear lock; emit ``order.cancelled``
        audit record.
      - rejected: clear lock; status restoration mirrors update.
    """
    db_action_raw = compound.get("db_action")
    if not db_action_raw:
        return  # legacy / internally-generated compound

    kind = db_action_raw["kind"]
    actor_id_raw = db_action_raw["actor_id"]
    actor_id = uuid.UUID(actor_id_raw) if actor_id_raw else None

    ops = compound.get("ops") or []
    if not ops:
        logger.warning("schedule.db_action.no_ops", compound_id=compound.get("compound_id"))
        return
    # All ops in a producer-generated compound target the same order_id.
    order_id = uuid.UUID(ops[0]["order_id"])

    db: Session = SessionLocal()
    try:
        order = db.scalars(select(Order).where(Order.id == order_id)).first()
        if order is None:
            logger.warning(
                "schedule.db_action.missing_order",
                order_id=str(order_id),
                kind=kind,
            )
            return

        if accepted:
            _apply_db_action_accept(db, order, kind, db_action_raw, actor_id)
        else:
            _apply_db_action_reject(order, kind)

        db.commit()
    finally:
        db.close()


def _apply_db_action_accept(
    db: Session,
    order: Order,
    kind: str,
    db_action: dict[str, Any],
    actor_id: uuid.UUID | None,
) -> None:
    """Write the success-path DB changes for an accepted compound.

    Audit logs are emitted here (not at producer time) so the audit
    timestamp reflects when the change was actually committed.
    """
    if kind == "create":
        # Producer already inserted the row + wrote the ``order.created``
        # audit. Worker only clears the in-flight lock so the row becomes
        # editable again; the materializer's apply_schedule will set
        # ``status=scheduled`` and fill the scheduling columns.
        order.is_processing_locked = False
        return

    if kind == "update":
        old_value: dict[str, Any] = {
            "wafer_quantity": order.wafer_quantity,
            "requested_delivery_date": str(order.requested_delivery_date),
            "notes": order.notes,
            "assigned_to": str(order.assigned_to) if order.assigned_to is not None else None,
        }
        new_qty = db_action.get("new_wafer_quantity")
        if new_qty is not None:
            order.wafer_quantity = int(new_qty)
        new_dl = db_action.get("new_requested_delivery_date")
        if new_dl is not None:
            order.requested_delivery_date = date.fromisoformat(str(new_dl))
        if db_action.get("new_notes_set"):
            order.notes = db_action.get("new_notes")
        if db_action.get("new_assigned_to_set"):
            raw_assignee = db_action.get("new_assigned_to")
            order.assigned_to = uuid.UUID(raw_assignee) if raw_assignee else None
        order.is_processing_locked = False
        new_value: dict[str, Any] = {
            "wafer_quantity": order.wafer_quantity,
            "requested_delivery_date": str(order.requested_delivery_date),
            "notes": order.notes,
            "assigned_to": str(order.assigned_to) if order.assigned_to is not None else None,
        }
        _worker_audit(
            db,
            action="order.updated",
            actor_id=actor_id,
            order_id=order.id,
            old_value=old_value,
            new_value=new_value,
        )
        return

    if kind == "delete":
        # N-5: capture the full pre-delete view in the audit row.
        # ``_build_delete_compound`` already snapshotted these into
        # ``db_action.old_*`` at producer time; before round-2 the worker
        # didn't read them and just logged ``status`` + ``is_deleted``,
        # making it impossible to answer "what was the qty / deadline /
        # notes when this order got cancelled?" without consulting the
        # full row history. Pulling the snapshot into the audit row makes
        # the history self-contained.
        old_value = {
            "status": order.status.value,
            "is_deleted": False,
            "wafer_quantity": db_action.get("old_wafer_quantity"),
            "requested_delivery_date": db_action.get("old_requested_delivery_date"),
            "notes": db_action.get("old_notes"),
            "assigned_to": db_action.get("old_assigned_to"),
        }
        order.is_deleted = True
        order.status = OrderStatus.cancelled
        order.is_processing_locked = False
        _worker_audit(
            db,
            action="order.cancelled",
            actor_id=actor_id,
            order_id=order.id,
            old_value=old_value,
        )
        return


def _apply_db_action_reject(order: Order, kind: str) -> None:
    """Compensate for a rejected compound: clear lock; for create, orphan-cleanup.

    For update/delete the DB columns the user wanted to change were
    never written by the producer, so "rollback" reduces to clearing
    the lock and snapping ``status`` back to whatever the row's actual
    schedule presence implies (scheduled vs pending). For create the
    producer DID write a stub row, so we soft-delete it.

    N-4 round-2 guard: never demote ``in_production`` here. Today the
    producer-side ``MUTABLE_STATUSES`` check already blocks PATCH /
    DELETE on an in-production row, so this branch is unreachable in
    practice. But if a future change opens up partial mutation (e.g.
    "you can change notes on a row that's mid-production"), the
    unconditional ``order.status = scheduled`` write below would
    silently demote the row mid-shift and break the same downstream
    invariant ``set_schedule_dates`` defends against (§4.4). Cheaper
    to defend here than to forget when MUTABLE_STATUSES is relaxed.
    """
    if kind == "create":
        order.is_deleted = True
        order.status = OrderStatus.cancelled
        order.is_processing_locked = False
        return

    # update / delete: just unlock and restore status.
    order.is_processing_locked = False
    if order.status == OrderStatus.in_production:
        # Defensive — see docstring.
        return
    if order.scheduled_production_date is not None:
        order.status = OrderStatus.scheduled
    else:
        order.status = OrderStatus.pending


def _worker_audit(
    db: Session,
    *,
    action: str,
    actor_id: uuid.UUID | None,
    order_id: uuid.UUID,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    """Write an audit row + ECS stdout record from inside the worker.

    Mirrors ``order_service._write_audit`` but takes an ``actor_id``
    directly (not a ``User`` row) because the worker doesn't load the
    user — the id rides in the compound's ``db_action`` payload.
    """
    audit_log_repo.create(
        db,
        action=action,
        user_id=actor_id,
        resource_type="order",
        resource_id=order_id,
        old_value=old_value,
        new_value=new_value,
    )
    emit_audit_log(
        action=action,
        actor_id=str(actor_id) if actor_id else None,
        resource_type="order",
        resource_id=str(order_id),
        changes={"old": old_value, "new": new_value},
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _finalize_run(state: SchedulerState) -> int:
    """Compute schedule, persist to DB, save state, broadcast.

    Shared by the three tasks that mutate state — ``run_scheduling_task``
    (per-op), ``advance_day_task`` (after rolling the horizon), and
    ``rebuild_schedule_task`` (after rebuilding from DB). Each of these is
    the only writer of ``schedule:state`` in its body, so calling this once
    at the end is correct.

    The pinned-orders map is derived from ``state.pinned_orders`` and
    threaded through to ``apply_schedule`` so DB rows for pinned orders end
    up with ``is_pinned=true`` and ``pinned_production_date=fake_deadline``.

    Returns the number of scheduled rows written, for logging.
    """
    scheduled = compute_schedule(state)
    pinned_map = {p.order_id: p.fake_deadline for p in state.pinned_orders.values()}

    db: Session = SessionLocal()
    try:
        order_service.apply_schedule(db, scheduled, pinned_map)
    finally:
        db.close()

    _save_state(state)
    websocket.broadcast({"type": "schedule.updated"})
    return len(scheduled)


@celery_app.task(bind=True, name="scheduling.run")  # type: ignore[untyped-decorator]
def run_scheduling_task(self: Task) -> None:  # noqa: PLR0915 — orchestration function: long but linear, extracting helpers hurts readability
    """Drain the pending queue via batch admission, then flip back to idle.

    **Phase 4 fast/slow split** still applies: this task is the fast path,
    doing in-memory state mutation + ``_save_state`` to Redis + WS
    ``compound_accepted`` per accepted compound; DB rewrites are
    offloaded to ``materialize_schedule_task`` which self-coalesces.

    **Batch admission (rewrite, supersedes the per-compound design)**:

    Each task invocation:

    1. Acquire ``schedule:state_writer_lock`` (single-flight). If held,
       silently skip — the holder will pick up our work on its drain loop.
    2. Set ``schedule:status`` to ``running``.
    3. **Drain loop** — repeat until the pending queue is empty:
       a. Read all pending compounds in priority order
          (``_read_pending_compounds``, ZRANGE without popping).
       b. Halving search for the largest feasible prefix
          (``_largest_halving_feasible_prefix``: tries ``[1..N]``,
          ``[1..N//2]``, ``[1..N//4]``, ..., ``[1..1]``).
       c. If the largest feasible attempt is **0** (even the first
          compound alone is infeasible), drop just that compound via
          ``_reject_first_compound`` and continue the drain loop.
       d. Otherwise commit the accepted prefix in one shot via
          ``_commit_accepted_batch`` (batch tree updates + per-compound
          pq/pin + ZREM + save + DB + WS), then continue.
    4. After draining, dispatch one ``materialize_schedule_task`` so
       fresh ``daily_breakdown`` / ``scheduled_production_date`` rows
       reflect the accepted state.
    5. Flip status to ``idle``.
    6. If new compounds arrived during step 4-5, re-trigger (unless a
       waiter has the flag set, in which case yield to it).

    **Why drain in one task rather than re-fire per batch**: each batch
    commit already touches Redis (ZREM + SAVE + per-compound HDEL), the
    Celery dispatch overhead would dominate. The drain loop bounds tail
    latency only by the queue depth itself; under a sustained
    enqueue-faster-than-drain workload the lock holder eventually
    re-triggers anyway via step 6.

    **No saga / rollback**: ``is_batch_feasible`` is checked BEFORE any
    tree mutation. An accepted batch is committed as a single atomic
    unit at the segment-tree level; pin/unpin failures in the structural
    pass are logged but not propagated (defensive — producer admission
    control vets fake_deadline upstream).

    Exception handling unchanged: any Python exception flips status to
    ``failed`` and re-raises so Celery records the traceback;
    ``schedule:status="failed"`` does NOT block ``/trigger`` calls
    because the 409 path only checks ``running``.
    """
    started_at = datetime.now(tz=UTC).isoformat()
    task_id = str(self.request.id) if self.request.id else None
    lock_holder_id = task_id or f"run-{uuid.uuid4()}"

    # P0-2: only one task at a time may mutate ``schedule:state``. If the
    # lock is held, the holder will pick up our compound on its drain
    # loop or self-retrigger — silently skip without touching status.
    if not _try_acquire_state_lock(lock_holder_id):
        logger.info("schedule.run.skip_lock_held", task_id=task_id)
        return

    _set_status(state=_STATUS_RUNNING, started_at=started_at, task_id=task_id)
    logger.info("schedule.run.start", task_id=task_id)

    try:
        any_batch_committed = False
        any_compound_rejected = False

        while True:
            # Reject-rate adaptive cap: only consider the first ``ceil(1/p)``
            # candidates for halving. With p tracking the per-compound
            # reject rate, this is the expected position of the next reject,
            # so prefixes beyond it are unlikely to be feasible anyway —
            # skipping them avoids paying ``compute_batch_capacity_delta``
            # on doomed candidates. ``_update_reject_rate`` keeps p current
            # below, so the cap auto-adapts to the workload. Reading p
            # BEFORE the pending fetch lets us bound the ZRANGE itself to
            # ``ceil(1/p)`` entries — saves O(N) bandwidth on long queues.
            rate = _get_reject_rate()
            read_cap = _take_count_from_rate(pending_count=10**9, rate=rate)
            pending = _read_pending_compounds(limit=read_cap)
            if not pending:
                break

            state = _load_state()
            take = _take_count_from_rate(len(pending), rate)
            candidates = pending[:take]

            k, attempts_tried = _largest_halving_feasible_prefix(
                state, [c for _, c in candidates]
            )
            # Halving rounds that failed before the successful one (or all
            # rounds, if k == 0) feed the EWMA as "this many prefix sizes
            # were rejected" — so the cap moves up when halving had to
            # retreat repeatedly, even if some compounds ended up accepted.
            halving_misses = max(0, attempts_tried - (1 if k > 0 else 0))

            if k == 0:
                first_member, first_compound = pending[0]
                _reject_first_compound(first_member, first_compound)
                # Count the dropped head as one real rejection; halving_misses
                # are extra hint pressure from the failed probes.
                _update_reject_rate(accepted=0, rejected=1 + halving_misses)
                any_compound_rejected = True
                continue

            _commit_accepted_batch(state, candidates[:k])
            _update_reject_rate(accepted=k, rejected=halving_misses)
            any_batch_committed = True

        if any_batch_committed:
            # Dispatch one materializer for the whole drain so DB rows
            # catch up to the accepted state. The race-guard sentinel is
            # only needed for advance_day / rebuild (which run concurrent
            # to an in-flight materializer); fast-path SADDs in
            # ``enqueue_notify_user`` already cover the post-release
            # re-trigger contract for our case.
            materialize_schedule_task.delay()

        if not any_batch_committed and not any_compound_rejected:
            logger.info("schedule.run.empty_queue", task_id=task_id)

        finished_at = datetime.now(tz=UTC).isoformat()
        _set_status(
            state=_STATUS_IDLE,
            started_at=started_at,
            finished_at=finished_at,
            task_id=task_id,
        )

        # If new compounds arrived between the last drain-loop read and
        # the status flip (window where producers see ``idle`` and SADD
        # without re-dispatching us themselves), re-fire so they don't
        # sit idle until external pressure. Yield to a waiter (advance_day
        # / rebuild) if its flag is set, otherwise self-dispatch.
        if cast("int", _get_redis().zcard(PENDING_OPS_KEY)) > 0:
            if _is_waiter_pending():
                logger.info("schedule.run.yield_to_waiter", task_id=task_id)
            else:
                logger.info("schedule.run.re_trigger", task_id=task_id)
                run_scheduling_task.delay()
    except Exception as exc:
        finished_at = datetime.now(tz=UTC).isoformat()
        _set_status(
            state=_STATUS_FAILED,
            started_at=started_at,
            finished_at=finished_at,
            task_id=task_id,
            error=str(exc),
        )
        logger.error(
            "schedule.run.failed",
            task_id=task_id,
            error=str(exc),
            exc_info=True,
        )
        raise
    finally:
        _release_state_lock(lock_holder_id)


# ---------------------------------------------------------------------------
# materialize_schedule_task — Phase 4 deferred DB writer
# ---------------------------------------------------------------------------


_MATERIALIZE_RUNNING_TTL_SECONDS = 300  # 5 min safety window for crash recovery

# System sentinel SADD'd into ``MATERIALIZE_NOTIFY_PENDING_KEY`` by
# advance_day_task / rebuild_schedule_task before they dispatch their
# follow-up materializer. Closes the race where an in-flight materializer
# (M1) is mid-``apply_schedule`` with pre-advance_day state when advance_day
# commits + dispatches M2 — M2 hits ``skip_concurrent`` (M1 still holds
# ``MATERIALIZE_RUNNING_KEY``), and M1's post-release re-trigger check at
# the bottom of ``materialize_schedule_task`` would otherwise see an empty
# pending set and not fire. With the sentinel present, M1's post-release
# check sees pending and dispatches a fresh M3 that picks up the post-
# advance_day state and overwrites the stale ``daily_breakdown``.
#
# Materializer's per-member ``uuid.UUID(user_raw)`` parse fails on this
# value and hits the ``except ValueError`` log-and-skip branch — so the
# sentinel costs at most one ``schedule.materialize.bad_user_id`` warning
# per advance_day / rebuild cycle and never reaches ``notify_user``.
_MATERIALIZE_SYSTEM_SENTINEL = "__system_advance_day__"


@celery_app.task(name="scheduling.materialize")  # type: ignore[untyped-decorator]
def materialize_schedule_task() -> None:
    """Drain pending notifications and write the schedule to DB.

    Phase 4 slow path. Self-coalescing: many ``run_scheduling_task`` fast-
    path successes can collapse into one materializer run, so a burst of N
    compounds doesn't cause N full DB rewrites.

    Coordination via three Redis keys (see ``services.scheduling`` for the
    constants):

    * ``MATERIALIZE_RUNNING_KEY``: ``SET NX EX 300``. Only one materializer
      runs at a time. If we don't get the slot, exit silently — the
      currently-running task will re-trigger us if needed.
    * ``MATERIALIZE_NOTIFY_PENDING_KEY``: SADD'd by ``run_scheduling_task``
      with each successful compound's ``requested_by``. The set is the
      "to-notify" backlog.
    * ``MATERIALIZE_NOTIFY_PROCESSING_KEY``: in-flight batch. Atomic swap
      via ``RENAME`` ensures the worker drains a consistent snapshot;
      concurrent fast-path SADDs land in a fresh notify_pending set and
      get picked up on the next loop iteration (or a re-triggered run).

    Loop body per iteration:

    1. ``RENAME notify_pending → notify_processing``. If pending was empty
       the rename raises ``ResponseError`` and we exit the loop.
    2. Read the captured user IDs (``SMEMBERS notify_processing``).
    3. ``_load_state()`` → ``compute_schedule(state)`` →
       ``order_service.apply_schedule(db, scheduled, pinned_map)``. Writes
       the latest in-memory schedule to DB.
    4. For each captured user: ``notify_user(schedule.materialized)`` so
       their frontend refetches ``GET /schedule/result`` and sees the
       fresh DB rows.
    5. ``DEL notify_processing``.
    6. Loop again — concurrent fast tasks may have repopulated
       notify_pending while we were working.

    On exception inside the loop, the in-flight batch is merged back into
    ``notify_pending`` (``SUNIONSTORE``) so we don't lose users on retry.

    On normal exit (loop break), release the running flag and check one
    last time — if more arrived between the loop's final empty rename and
    our flag release, ``.delay()`` ourselves so the next worker picks
    them up.
    """
    rds = _get_redis()

    # Single-flight claim. Multiple delayed invocations cooperate via NX.
    claimed = rds.set(
        MATERIALIZE_RUNNING_KEY,
        "1",
        nx=True,
        ex=_MATERIALIZE_RUNNING_TTL_SECONDS,
    )
    if not claimed:
        logger.info("schedule.materialize.skip_concurrent")
        return

    try:
        # Crash recovery: salvage anything left in notify_processing from a
        # previous run that died mid-iteration. SUNIONSTORE merges the two
        # sets, DEL clears the processing key.
        if rds.exists(MATERIALIZE_NOTIFY_PROCESSING_KEY):
            rds.sunionstore(
                MATERIALIZE_NOTIFY_PENDING_KEY,
                [MATERIALIZE_NOTIFY_PENDING_KEY, MATERIALIZE_NOTIFY_PROCESSING_KEY],
            )
            rds.delete(MATERIALIZE_NOTIFY_PROCESSING_KEY)

        while True:
            try:
                rds.rename(
                    MATERIALIZE_NOTIFY_PENDING_KEY,
                    MATERIALIZE_NOTIFY_PROCESSING_KEY,
                )
            except ResponseError:
                # notify_pending didn't exist → no work this iteration.
                break

            users_raw = cast(
                "set[str]",
                rds.smembers(MATERIALIZE_NOTIFY_PROCESSING_KEY),
            )

            try:
                # P3-3: defensive empty-state guard. If ``schedule:state``
                # doesn't exist yet (extreme race: fast-path task enqueued
                # a notify before its ``_save_state`` landed, or Redis was
                # flushed between fast-path success and our wake-up), we'd
                # otherwise call ``apply_schedule`` with an empty
                # ``ScheduledResult`` list — which wipes every order's
                # daily_breakdown / scheduled_production_date in DB
                # (``clear_scheduled_dates`` at the top of apply_schedule).
                # That's a real-data-loss bug. Detecting the missing key
                # and pushing the captured users back onto pending_notify
                # is the safe move; the next materializer (triggered by
                # whichever task eventually writes state) will pick them
                # up with a real state in hand.
                if not rds.exists(STATE_KEY):
                    logger.warning(
                        "schedule.materialize.skip_no_state",
                        notify_users=len(users_raw),
                    )
                    rds.sunionstore(
                        MATERIALIZE_NOTIFY_PENDING_KEY,
                        [MATERIALIZE_NOTIFY_PENDING_KEY, MATERIALIZE_NOTIFY_PROCESSING_KEY],
                    )
                    rds.delete(MATERIALIZE_NOTIFY_PROCESSING_KEY)
                    break

                # Materializer is deliberately NOT in ``state_writer_lock``.
                # ``schedule:status`` (idle / running) is the lifecycle key
                # of ``run_scheduling_task`` — letting a slow path
                # (apply_schedule per N orders, ~4 ms/order) gate that
                # would block user-facing PATCH/DELETE latency on
                # something the user doesn't care about. The lock is
                # reserved for tasks that actually mutate
                # ``schedule:state`` (run / advance_day / rebuild).
                #
                # The trade-off this re-introduces: if advance_day saves
                # a new state between our ``_load_state`` and our
                # ``apply_schedule``, we'll write DB with the pre-
                # advance_day view → ``daily_breakdown`` /
                # ``scheduled_production_date`` go stale by one
                # materializer cycle. ``status`` columns (``in_production``
                # set by ``mark_in_production``, ``completed`` set by
                # ``mark_completed_outside_set``) are NOT touched by our
                # ``apply_schedule`` because ``set_schedule_dates``
                # preserves ``in_production`` and never writes
                # ``completed``, so advance_day's status work is safe.
                #
                # advance_day_task / rebuild_schedule_task explicitly
                # dispatch a fresh materializer after their commit so
                # the stale window is bounded by one materializer cycle
                # (≈ 50ms-2s) rather than "until the next user
                # compound", even on a quiet day.
                state = _load_state()
                scheduled_results = compute_schedule(state)
                pinned_map = {p.order_id: p.fake_deadline for p in state.pinned_orders.values()}
                db: Session = SessionLocal()
                try:
                    order_service.apply_schedule(db, scheduled_results, pinned_map)
                finally:
                    db.close()

                for user_raw in users_raw:
                    try:
                        websocket.notify_user(
                            user_id=uuid.UUID(user_raw),
                            message={"type": "schedule.materialized"},
                        )
                    except ValueError:
                        # Bad UUID slipped into the set somehow — log and
                        # move on, don't block the batch on one bad entry.
                        logger.warning(
                            "schedule.materialize.bad_user_id",
                            user=user_raw,
                        )

                rds.delete(MATERIALIZE_NOTIFY_PROCESSING_KEY)
                logger.info(
                    "schedule.materialize.batch_done",
                    notified=len(users_raw),
                    scheduled_rows=len(scheduled_results),
                )
            except Exception:
                # Restore the batch so the next run can retry. We don't
                # want to silently lose users on a transient DB outage.
                rds.sunionstore(
                    MATERIALIZE_NOTIFY_PENDING_KEY,
                    [MATERIALIZE_NOTIFY_PENDING_KEY, MATERIALIZE_NOTIFY_PROCESSING_KEY],
                )
                rds.delete(MATERIALIZE_NOTIFY_PROCESSING_KEY)
                raise
    finally:
        rds.delete(MATERIALIZE_RUNNING_KEY)

    # Post-release check: a fast task may have SADD'd between our final
    # empty rename and the running-flag DEL. Re-trigger so the new pending
    # users don't sit idle until the next fast task fires its own .delay().
    if rds.exists(MATERIALIZE_NOTIFY_PENDING_KEY):
        logger.info("schedule.materialize.re_trigger")
        materialize_schedule_task.delay()


def _wait_for_idle_run(*, log_event: str) -> None:
    """Block until ``schedule:status`` is no longer ``running``, or 5 min elapse.

    Shared by ``advance_day_task`` and ``rebuild_schedule_task`` — both must
    let an in-flight ``run_scheduling_task`` finish before they mutate state.
    Times out and proceeds anyway after 5 minutes; better to act on slightly
    stale state than to skip the operation entirely.
    """
    deadline = time.monotonic() + _RUN_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = _get_status()
        if status is None or status.get("state") != _STATUS_RUNNING:
            return
        time.sleep(_RUN_WAIT_POLL_INTERVAL_SECONDS)
    logger.warning(log_event, timeout_seconds=_RUN_WAIT_TIMEOUT_SECONDS)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="scheduling.advance_day",
    # N-2 round-2: if state_writer_lock can't be grabbed within the full
    # timeout, the body raises ``RuntimeError("could not acquire state-writer
    # lock within ...s")``. Without autoretry that single failure means the
    # whole day's ``mark_in_production`` / ``mark_completed_outside_set`` /
    # apply_schedule never runs — order status workflow stalls until the
    # next Beat firing 24h later. Three retries with exponential backoff
    # (60s, 120s, 240s by default — capped at ``retry_backoff_max``) cover
    # transient contention windows; if it still fails after that, status=
    # failed surfaces on ``GET /schedule/status`` so ops can intervene
    # before another 24h tick.
    autoretry_for=(RuntimeError,),
    max_retries=3,
    retry_backoff=60,
    retry_backoff_max=600,
    retry_jitter=True,
)
def advance_day_task() -> None:
    """Roll the scheduler horizon forward one day at 00:00 UTC.

    Polls ``schedule:status`` for up to 5 minutes waiting for any in-flight
    ``run_scheduling_task`` to finish. After the wait window we proceed
    regardless — better to re-base on a slightly stale state than to skip
    the day rollover entirely.

    **Phase 3 — DB status workflow**. At each fire the task does two extra
    UPDATEs alongside the existing finalize:

    1. **``in_production → completed``** for orders that WERE locked in
       on a previous day and are no longer in the new scheduler state's
       living set (= ``pq_index + pinned_orders``). Signal: those
       orders' last remaining work was made yesterday, they're done.
    2. **(scheduled | …) → ``in_production``** for orders that have any
       production assigned to "today" (the day-1 of the OLD state, which
       advance_day removes / partially consumes). This includes pinned-
       today, fully-done pq, and boundary orders. We snapshot the
       today-set from ``compute_schedule(old_state)`` BEFORE running
       advance_day, since after advance_day base_date has shifted and
       day-1 of new_state is tomorrow.

    The override order is: ``apply_schedule`` first (writes
    ``scheduled_production_date`` / ``expected_delivery_date`` /
    ``status='scheduled'`` for orders in the new state) → then
    ``mark_completed_outside_set`` → then ``mark_in_production`` last (so
    boundary orders which apply_schedule wrote as ``scheduled`` get
    correctly upgraded to ``in_production``).

    Sets the waiter flag for the entire duration (set before
    ``_wait_for_idle_run``, cleared in outer ``finally``) so a concurrent
    ``run_scheduling_task`` finishing during our wait yields its re-trigger
    to us instead of racing.

    Also claims ``schedule:status = running`` for the duration. On clean
    completion the inner success path writes ``idle``; on any exception the
    inner ``except`` writes ``failed`` (with the error string captured in
    ``schedule:status.error``) and re-raises so Celery still records the
    traceback.
    """
    logger.info("schedule.advance_day.start")
    _set_waiter_flag()
    lock_holder_id = f"advance_day-{uuid.uuid4()}"
    lock_acquired = False
    try:
        _wait_for_idle_run(log_event="schedule.advance_day.wait_timeout")

        # P0-3: serialize state writes against ``run_scheduling_task``. The
        # waiter_pending flag is advisory and only guides re-trigger
        # behavior — it doesn't prevent a fresh run_task from grabbing
        # the lock between our wait completing and our state mutation.
        # Bounded wait because we MUST run; if the lock is genuinely held
        # for the whole TTL, that's already a sign something is wrong and
        # raising into ``failed`` status is the right escalation.
        lock_acquired = _acquire_state_lock_blocking(
            lock_holder_id, timeout_seconds=_RUN_WAIT_TIMEOUT_SECONDS
        )
        if not lock_acquired:
            logger.error("schedule.advance_day.lock_timeout", task_id=lock_holder_id)
            raise RuntimeError(
                "advance_day_task could not acquire state-writer lock within "
                f"{_RUN_WAIT_TIMEOUT_SECONDS}s"
            )

        started_at = datetime.now(tz=UTC).isoformat()
        _set_status(state=_STATUS_RUNNING, started_at=started_at)
        try:
            state = _load_state()

            # Snapshot "today's locked-in" set BEFORE advance_day shifts
            # state. compute_schedule(state) gives us every order with any
            # work on day-1 of the OLD state (= today by definition of the
            # 00:00 UTC fire). Filter by scheduled_date == state.base_date
            # to be explicit.
            today = state.base_date
            today_locked_in_ids: set[uuid.UUID] = {
                sr.order_id for sr in compute_schedule(state) if sr.scheduled_date == today
            }

            new_state = advance_day(state)

            # Orders still alive in the new state — used by
            # ``mark_completed_outside_set`` to decide which currently-
            # in_production rows are done.
            new_alive_ids: set[uuid.UUID] = set(new_state.pq_index.keys()) | set(
                new_state.pinned_orders.keys()
            )

            # Combined DB workflow: apply_schedule + status flips. We bypass
            # ``_finalize_run`` here so we can serialize the three DB writes
            # in one session before ``_save_state`` + broadcast.
            scheduled_results = compute_schedule(new_state)
            pinned_map = {p.order_id: p.fake_deadline for p in new_state.pinned_orders.values()}
            db: Session = SessionLocal()
            try:
                # 1. Set scheduled_production_date / dates / pin columns
                #    for orders still in state (status=scheduled).
                order_service.apply_schedule(db, scheduled_results, pinned_map)
                # 2. Yesterday's in_production orders that have no remaining
                #    work in state → completed.
                completed_count = order_repo.mark_completed_outside_set(db, new_alive_ids)
                # 3. Today's-locked-in (advance_day moved them out of state)
                #    → in_production. Overrides apply_schedule's
                #    ``scheduled`` for any boundary order present here.
                in_prod_count = order_repo.mark_in_production(db, today_locked_in_ids)
                db.commit()
            finally:
                db.close()

            _save_state(new_state)
            websocket.broadcast({"type": "schedule.updated"})

            logger.info(
                "schedule.advance_day.success",
                old_base=state.base_date.isoformat(),
                new_base=new_state.base_date.isoformat(),
                carried=len(new_state.pq_index),
                in_production_count=in_prod_count,
                completed_count=completed_count,
            )

            # Drain any pending compounds on top of the new state. If the
            # queue is empty, finalize already broadcast and we have nothing
            # more to do.
            if cast("int", _get_redis().zcard(PENDING_OPS_KEY)) > 0:
                run_scheduling_task.delay()

            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_IDLE,
                started_at=started_at,
                finished_at=finished_at,
            )
        except Exception as exc:
            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_FAILED,
                started_at=started_at,
                finished_at=finished_at,
                error=str(exc),
            )
            logger.error(
                "schedule.advance_day.failed",
                error=str(exc),
                exc_info=True,
            )
            raise
    finally:
        if lock_acquired:
            _release_state_lock(lock_holder_id)
        _clear_waiter_flag()
        # Dispatch a fresh materializer after we release the lock so
        # any in-flight materializer that read pre-advance_day state
        # (and is about to write stale ``daily_breakdown`` /
        # ``scheduled_production_date``) gets overwritten by the next
        # materialize cycle. Bounds the stale window to one materializer
        # latency instead of "until the next user compound". See the
        # comment block inside ``materialize_schedule_task`` for why
        # we accept the staleness instead of locking against it.
        #
        # The SADD before .delay() is load-bearing: if the in-flight
        # materializer is still holding ``MATERIALIZE_RUNNING_KEY`` when
        # our M2 wakes up, M2 hits ``skip_concurrent`` and returns. The
        # sentinel guarantees the in-flight materializer's post-release
        # check (``rds.exists(MATERIALIZE_NOTIFY_PENDING_KEY)``) sees
        # work and dispatches a fresh M3. See ``_MATERIALIZE_SYSTEM_SENTINEL``
        # for the parse-failure path on the materializer side.
        _get_redis().sadd(MATERIALIZE_NOTIFY_PENDING_KEY, _MATERIALIZE_SYSTEM_SENTINEL)
        materialize_schedule_task.delay()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="scheduling.rebuild",
    # N-2: same autoretry policy as ``advance_day_task``. Rebuild is
    # user-triggered (``POST /schedule/rebuild`` from ops) so a lock-
    # timeout is recoverable by retry — better than handing back a stale
    # ``status=failed`` and letting ops re-click manually.
    autoretry_for=(RuntimeError,),
    max_retries=3,
    retry_backoff=60,
    retry_backoff_max=600,
    retry_jitter=True,
)
def rebuild_schedule_task() -> None:
    """Rebuild scheduler state from DB on top of the latest base_date.

    Like ``advance_day_task`` this waits for any in-flight ``run_scheduling_task``
    to finish first (so we don't race state writes), rebuilds from
    ``status='scheduled'`` rows in Postgres, persists the new state, fires a
    ``schedule.rebuild_skipped`` WebSocket notification to each skipped order's
    creator, then re-triggers ``run_scheduling_task`` so any pending ops
    queued during the wait get drained on top of the fresh state.

    Skipped orders are only those whose ``requested_delivery_date`` falls
    outside the 30-day horizon — this can happen for long-stuck scheduled
    orders whose deadline has been overtaken by ``base_date`` advancing.

    Status handling mirrors ``advance_day_task``: clean exit writes ``idle``,
    any exception inside the body writes ``failed`` with the error message
    and re-raises so Celery records the traceback. See ``advance_day_task``
    docstring for the rationale.
    """
    logger.info("schedule.rebuild.start")
    _set_waiter_flag()
    lock_holder_id = f"rebuild-{uuid.uuid4()}"
    lock_acquired = False
    try:
        _wait_for_idle_run(log_event="schedule.rebuild.wait_timeout")

        # P0-3: serialize state writes against ``run_scheduling_task``. Same
        # rationale as ``advance_day_task``.
        lock_acquired = _acquire_state_lock_blocking(
            lock_holder_id, timeout_seconds=_RUN_WAIT_TIMEOUT_SECONDS
        )
        if not lock_acquired:
            logger.error("schedule.rebuild.lock_timeout", task_id=lock_holder_id)
            raise RuntimeError(
                "rebuild_schedule_task could not acquire state-writer lock within "
                f"{_RUN_WAIT_TIMEOUT_SECONDS}s"
            )

        started_at = datetime.now(tz=UTC).isoformat()
        _set_status(state=_STATUS_RUNNING, started_at=started_at)
        try:
            raw = cast("str | None", _get_redis().get(STATE_KEY))
            if raw is not None:
                base_date = SchedulerState.from_json(raw).base_date
            else:
                base_date = datetime.now(tz=UTC).date()

            db: Session = SessionLocal()
            try:
                orders, creators = order_service.list_for_scheduler(db)
            finally:
                db.close()

            new_state, skipped = rebuild_state(orders, base_date)
            _finalize_run(new_state)

            for skip in skipped:
                creator_id = creators.get(skip.order_id)
                if creator_id is None:
                    continue
                websocket.notify_user(
                    user_id=creator_id,
                    message={
                        "type": "schedule.rebuild_skipped",
                        "order_id": str(skip.order_id),
                        "order_number": skip.order_number,
                        "reason": skip.reason,
                    },
                )

            logger.info(
                "schedule.rebuild.success",
                base_date=base_date.isoformat(),
                orders_added=len(new_state.pq_index),
                orders_skipped=len(skipped),
            )

            # Drain any pending ops on top of the rebuilt state. If empty,
            # finalize already broadcast and we have nothing more to do.
            if cast("int", _get_redis().zcard(PENDING_OPS_KEY)) > 0:
                run_scheduling_task.delay()

            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_IDLE,
                started_at=started_at,
                finished_at=finished_at,
            )
        except Exception as exc:
            finished_at = datetime.now(tz=UTC).isoformat()
            _set_status(
                state=_STATUS_FAILED,
                started_at=started_at,
                finished_at=finished_at,
                error=str(exc),
            )
            logger.error(
                "schedule.rebuild.failed",
                error=str(exc),
                exc_info=True,
            )
            raise
    finally:
        if lock_acquired:
            _release_state_lock(lock_holder_id)
        _clear_waiter_flag()
        # Same as advance_day_task: refresh materialized DB columns
        # to overwrite any racing materializer that read the pre-
        # rebuild state. See ``materialize_schedule_task`` body for
        # the staleness trade-off rationale, and the matching block in
        # advance_day_task for why we SADD a sentinel before .delay().
        _get_redis().sadd(MATERIALIZE_NOTIFY_PENDING_KEY, _MATERIALIZE_SYSTEM_SENTINEL)
        materialize_schedule_task.delay()
