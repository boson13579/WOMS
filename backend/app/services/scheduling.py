"""Pure scheduling algorithm for wafer order production.

This module owns the segment-tree, priority-queue, and EDF logic that
underpin daily scheduling. It is deliberately persistence-free: no DB,
no Redis, no FastAPI.

Responsibilities
----------------
- ``SegmentTree``: prefix-sum segment tree with lazy range-set and point-update.
- ``SchedulerState``: Pydantic snapshot serializable to/from JSON for Redis.
- ``add_order`` / ``remove_order``: mutate state for a single order, updating
  both trees so that capacity feasibility stays in sync with the priority queue.
- ``compute_schedule``: derive a per-day per-order assignment list (forward fill).
- ``advance_day``: roll the horizon forward one day and prune completed work.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Literal, NamedTuple

import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

__all__ = [
    "DAILY_CAPACITY",
    "HORIZON_DAYS",
    "MATERIALIZE_NOTIFY_PENDING_KEY",
    "MATERIALIZE_NOTIFY_PROCESSING_KEY",
    "MATERIALIZE_RUNNING_KEY",
    "PENDING_OPS_KEY",
    "PENDING_OPS_SEQ_KEY",
    "STATE_KEY",
    "STATUS_KEY",
    "BatchOp",
    "PinnedOrder",
    "ScheduleResult",
    "ScheduledResult",
    "SchedulerState",
    "SchedulingOrder",
    "SegmentTree",
    "SkippedOrder",
    "abs_to_rel",
    "add_order",
    "advance_day",
    "apply_batch_to_capacity",
    "apply_batch_to_deadline",
    "capacity_prefix_sums",
    "compute_batch_capacity_delta",
    "compute_schedule",
    "is_batch_feasible",
    "pin_order",
    "pq_add",
    "pq_remove",
    "rebuild_state",
    "rel_to_abs",
    "remove_order",
    "score_for_op",
    "unpin_order",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
#
# Sourced from environment via :class:`app.core.config.Settings` so deployments
# can tune them without code changes (see ``SCHEDULER_DAILY_CAPACITY`` /
# ``SCHEDULER_HORIZON_DAYS`` in ``.env.example``). Snapshotted at import time
# into module-level names that the rest of the codebase imports as
# ``DAILY_CAPACITY`` / ``HORIZON_DAYS``; tests can monkey-patch these names
# directly. Changing these in a running deployment requires a worker restart
# **and** a rebuild of any persisted ``schedule:state`` (the segment tree
# raw_values length is sized by HORIZON_DAYS).

_settings = get_settings()
DAILY_CAPACITY: int = _settings.SCHEDULER_DAILY_CAPACITY
HORIZON_DAYS: int = _settings.SCHEDULER_HORIZON_DAYS


# ---------------------------------------------------------------------------
# Redis-key + queue-encoding contract
# ---------------------------------------------------------------------------
#
# Redis keys and the ``score_for_op`` encoding live here — at the *services*
# layer — because they are the contract between the API producer and the
# Celery worker consumer. Both layers must agree on names and score format,
# but the API has no business reaching into ``workers/`` for them (RULES.md
# §3 forbids ``api/ → workers/`` for anything beyond Celery task dispatch).
# Putting the contract in services/ makes it the single canonical source.

STATE_KEY = "schedule:state"
"""Redis string key holding the JSON-serialized ``SchedulerState``."""

STATUS_KEY = "schedule:status"
"""Redis string key holding the worker lifecycle JSON document."""

PENDING_OPS_KEY = "schedule:pending_ops"
"""Redis sorted-set key for queued add/remove ops (ZADD/ZPOPMIN, O(log n))."""

PENDING_OPS_SEQ_KEY = "schedule:pending_ops:seq"
"""Redis monotonic counter (INCR) for the ``seq`` field embedded in op scores."""

# ---------------------------------------------------------------------------
# Materializer-side coordination keys (Phase 4 fast/slow split)
# ---------------------------------------------------------------------------
#
# ``run_scheduling_task`` (fast path) only mutates the in-memory ``SchedulerState``
# and saves the segment-tree + pq snapshot to Redis; it does NOT call
# ``apply_schedule`` (= DB write of every ``status='scheduled'`` row). DB
# materialization is offloaded to ``materialize_schedule_task`` so the
# producer's accept/reject feedback is O(log n)·N per compound rather than
# being gated on N DB round-trips.
#
# Coordination among the three keys below:
#   * Fast task SADDs the compound's ``requested_by`` into
#     MATERIALIZE_NOTIFY_PENDING_KEY and dispatches the materializer.
#   * Materializer claims MATERIALIZE_RUNNING_KEY (SET NX EX 300). Multiple
#     fast tasks coalesce into one materializer run.
#   * Materializer atomically RENAMEs notify_pending → notify_processing
#     (the rename is the "swap & drain" primitive: new SADDs land in a
#     fresh notify_pending set), then writes DB, then notifies the captured
#     users. On failure, notify_processing is merged back into
#     notify_pending so retries are lossless.

MATERIALIZE_NOTIFY_PENDING_KEY = "schedule:materialize_notify_pending"
"""Redis set of ``requested_by`` UUIDs awaiting materializer notification."""

MATERIALIZE_NOTIFY_PROCESSING_KEY = "schedule:materialize_notify_processing"
"""Redis set holding the in-flight batch the materializer is currently working on."""

MATERIALIZE_RUNNING_KEY = "schedule:materialize_running"
"""Redis SET-NX-EX flag indicating a materializer task is currently running."""

# Score layout for ``schedule:pending_ops``:
#   score = GROUP_OFFSET * group_priority + seq
# where ``group_priority`` is 0 for shrink-group ops (popped first) and 1 for
# grow-group ops, and ``seq`` is the ``PENDING_OPS_SEQ_KEY`` value (oldest op =
# smallest seq = popped first within its group).
#
# 10**12 is large enough that we'd need a trillion ops in the shrink group
# before colliding with the grow group's score range, while still well within
# float64's exact-integer range (2**53 ≈ 9.0e15) so ZPOPMIN ordering stays
# stable.
_GROUP_OFFSET = 10**12


def score_for_op(*, group: str, seq: int) -> float:
    """Compute the ZADD score for a pending op.

    Producer (``app.api.v1.schedule.enqueue_operation``) and consumer
    (``app.workers.scheduling._pop_next_op``) both call through this single
    helper so neither side has to know the encoding. Raises ``ValueError`` on
    an unknown group rather than silently picking a wrong score.
    """
    if group not in ("shrink", "grow"):
        raise ValueError(f"unknown pending-op group: {group!r}")
    return float((0 if group == "shrink" else 1) * _GROUP_OFFSET + seq)


# ---------------------------------------------------------------------------
# Date conversion
# ---------------------------------------------------------------------------


def abs_to_rel(absolute_date: date, base_date: date) -> int | None:
    """Convert an absolute date to a 1-based segment-tree index.

    Returns ``None`` when the date falls outside ``[base_date, base_date+29]``;
    callers treat that as "cannot be scheduled".
    """
    delta = (absolute_date - base_date).days
    if delta < 0 or delta >= HORIZON_DAYS:
        return None
    return delta + 1


def rel_to_abs(rel_index: int, base_date: date) -> date:
    """Convert a 1-based segment-tree index back to an absolute date."""
    return base_date + timedelta(days=rel_index - 1)


# ---------------------------------------------------------------------------
# SegmentTree
# ---------------------------------------------------------------------------


class SegmentTree:
    """Prefix-sum segment tree with lazy range-set and point-update.

    Externally 1-indexed over a fixed window of ``HORIZON_DAYS`` days.

    Operations
    ----------
    - ``query(i)``         -> prefix sum over [1, i].
    - ``range_set(l, r, v)`` -> overwrite every position in [l, r] with ``v``.
    - ``point_update(i, d)`` -> add ``d`` to the value at position ``i``.

    A leaf may carry a stale lazy tag after ``point_update`` (the tag is only
    authoritative on internal nodes until pushed); queries always read from
    ``_sum`` so this never affects correctness.
    """

    def __init__(self, n: int = HORIZON_DAYS, initial: int = 0) -> None:
        """Build a tree over [1, n] with every leaf initialized to ``initial``."""
        self._n = n
        self._sum: list[int] = [0] * (4 * n)
        self._lazy: list[int | None] = [None] * (4 * n)
        if initial != 0:
            self.range_set(1, n, initial)

    # ----- internal recursion --------------------------------------------

    def _apply_set(self, node: int, lo: int, hi: int, v: int) -> None:
        self._sum[node] = (hi - lo + 1) * v
        self._lazy[node] = v

    def _push(self, node: int, lo: int, hi: int) -> None:
        v = self._lazy[node]
        if v is None:
            return
        mid = (lo + hi) // 2
        self._apply_set(2 * node, lo, mid, v)
        self._apply_set(2 * node + 1, mid + 1, hi, v)
        self._lazy[node] = None

    def _range_set(self, node: int, lo: int, hi: int, lft: int, rgt: int, v: int) -> None:
        if rgt < lo or hi < lft:
            return
        if lft <= lo and hi <= rgt:
            self._apply_set(node, lo, hi, v)
            return
        self._push(node, lo, hi)
        mid = (lo + hi) // 2
        self._range_set(2 * node, lo, mid, lft, rgt, v)
        self._range_set(2 * node + 1, mid + 1, hi, lft, rgt, v)
        self._sum[node] = self._sum[2 * node] + self._sum[2 * node + 1]

    def _point_update(self, node: int, lo: int, hi: int, i: int, delta: int) -> None:
        if lo == hi:
            self._sum[node] += delta
            return
        self._push(node, lo, hi)
        mid = (lo + hi) // 2
        if i <= mid:
            self._point_update(2 * node, lo, mid, i, delta)
        else:
            self._point_update(2 * node + 1, mid + 1, hi, i, delta)
        self._sum[node] = self._sum[2 * node] + self._sum[2 * node + 1]

    def _query(self, node: int, lo: int, hi: int, lft: int, rgt: int) -> int:
        if rgt < lo or hi < lft:
            return 0
        if lft <= lo and hi <= rgt:
            return self._sum[node]
        self._push(node, lo, hi)
        mid = (lo + hi) // 2
        return self._query(2 * node, lo, mid, lft, rgt) + self._query(
            2 * node + 1, mid + 1, hi, lft, rgt
        )

    # ----- public API -----------------------------------------------------

    def query(self, i: int) -> int:
        """Return the prefix sum over [1, i]."""
        if not 1 <= i <= self._n:
            raise IndexError(f"query index {i} out of [1, {self._n}]")
        return self._query(1, 1, self._n, 1, i)

    def range_set(self, lft: int, rgt: int, v: int) -> None:
        """Overwrite every position in [lft, rgt] with the value ``v``."""
        if not (1 <= lft <= rgt <= self._n):
            raise IndexError(f"range_set([{lft}, {rgt}]) out of [1, {self._n}]")
        self._range_set(1, 1, self._n, lft, rgt, v)

    def point_update(self, i: int, delta: int) -> None:
        """Add ``delta`` to the value at position ``i``."""
        if not 1 <= i <= self._n:
            raise IndexError(f"point_update index {i} out of [1, {self._n}]")
        if delta != 0:
            self._point_update(1, 1, self._n, i, delta)

    # ----- serialization helpers -----------------------------------------

    def to_array(self) -> list[int]:
        """Materialize the per-day raw values (length = ``_n``)."""
        cumulative_prev = 0
        out: list[int] = []
        for i in range(1, self._n + 1):
            cumulative = self._query(1, 1, self._n, 1, i)
            out.append(cumulative - cumulative_prev)
            cumulative_prev = cumulative
        return out

    @classmethod
    def from_array(cls, values: list[int]) -> SegmentTree:
        """Reconstruct a tree from per-day raw values."""
        if len(values) != HORIZON_DAYS:
            raise ValueError(f"expected {HORIZON_DAYS} values, got {len(values)}")
        tree = cls(n=HORIZON_DAYS)
        for i, v in enumerate(values, start=1):
            if v:
                tree.point_update(i, v)
        return tree


# ---------------------------------------------------------------------------
# Pydantic data classes
# ---------------------------------------------------------------------------


class SchedulingOrder(BaseModel):
    """Order data needed by the scheduler — decoupled from the SQLAlchemy entity.

    ``pinned_production_date`` is set only when the DB row's ``is_pinned`` is
    true. ``rebuild_state`` reads it to decide whether the order should land
    in the priority queue (real deadline) or in ``pinned_orders`` (forced day
    = ``pinned_production_date``). At runtime — once state is in Redis and
    the worker is mutating it via ``add_order`` / ``pin_order`` — the field
    is irrelevant; pq orders are never pinned and pinned orders carry their
    pin-day in ``PinnedOrder.fake_deadline``.
    """

    order_id: uuid.UUID
    order_number: str
    wafer_quantity: int = Field(gt=0)
    deadline: date
    pinned_production_date: date | None = None

    def sort_key(self) -> tuple[date, int, str]:
        """Priority key: deadline early > wafer_quantity large > order_number lex."""
        return (self.deadline, -self.wafer_quantity, self.order_number)


class PinnedOrder(BaseModel):
    """An order that is locked to a specific production day.

    Pinned orders are NOT in ``pq_index`` — they live in
    ``SchedulerState.pinned_orders``. Segment trees still account for them,
    but indexed at ``fake_deadline`` (the pin day) rather than the real
    ``deadline``: that's what makes the same ``capacity_tree.query(...)``
    check work for both pq adds and pin operations.

    ``compute_schedule`` places the full ``wafer_quantity`` at
    ``fake_deadline`` (no spreading) before letting pq orders fill the
    remaining capacity. Unpinning re-creates a ``SchedulingOrder`` from
    ``deadline`` and pushes it back into pq.
    """

    order_id: uuid.UUID
    order_number: str
    wafer_quantity: int = Field(gt=0)
    deadline: date
    fake_deadline: date


class ScheduleResult(BaseModel):
    """Outcome of an ``add_order`` / ``remove_order`` call."""

    status: Literal["success", "capacity_exceeded", "deadline_too_far"]
    order_id: uuid.UUID | None = None
    message: str | None = None


class ScheduledResult(BaseModel):
    """A single (order, day, quantity) assignment from ``compute_schedule``."""

    order_id: uuid.UUID
    scheduled_date: date
    quantity: int = Field(gt=0)


class SkippedOrder(BaseModel):
    """An order that ``rebuild_state`` could not place back into the trees.

    The only realistic cause in normal operation is ``deadline_too_far`` —
    a previously-scheduled order whose ``requested_delivery_date`` has been
    passed by ``base_date`` due to elapsed time or migration into a system
    with a different time origin. Callers should surface this via WebSocket
    so the original requester knows the order needs intervention.
    """

    order_id: uuid.UUID
    order_number: str
    reason: Literal["capacity_exceeded", "deadline_too_far"]


class SchedulerState(BaseModel):
    """Live scheduler state; persisted to Redis as JSON between runs.

    Data structure choices for the live collections — see also
    ``docs/scheduling.md §4.1.2``:

    - ``pq_index`` is a ``dict[order_id, SchedulingOrder]`` holding the
      EDF priority queue (= every order not currently pinned). Pre-refactor
      this was a `SortedKeyList` kept sorted at every mutation — O(log n)
      per add / remove. Replaced with a flat dict because:
        - the only consumers that need EDF order (``compute_schedule`` /
          ``advance_day``) bucket-sort by ``deadline_rel`` at iteration
          time (see ``_iter_pq_edf_sorted``), which is O(n) for the bucket
          pass + O(K_d log K_d) for within-day tie-breaking (typically
          tiny since K_d is much less than n).
        - runtime ops (``add_order``, ``remove_order``, ``pin``, ``unpin``,
          batch admission's per-leaf ``pq_add``/``pq_remove``) drop from
          O(log n) to O(1).
        - serialization is just ``dict.values()`` → list, no SortedKeyList
          to rebuild on load.
    - ``pinned_orders`` is a ``dict[order_id, PinnedOrder]``. Python dicts
      preserve insertion order (3.7+) so iteration order is deterministic
      for tests / WS replay, while still giving O(1) ``contains`` / ``remove``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    capacity_tree: SegmentTree
    deadline_tree: SegmentTree
    pq_index: dict[uuid.UUID, SchedulingOrder] = Field(default_factory=dict)
    pinned_orders: dict[uuid.UUID, PinnedOrder] = Field(default_factory=dict)
    base_date: date

    @classmethod
    def initial(cls, base_date: date) -> SchedulerState:
        """Empty state with full daily capacity and no scheduled orders."""
        return cls(
            capacity_tree=SegmentTree(n=HORIZON_DAYS, initial=DAILY_CAPACITY),
            deadline_tree=SegmentTree(n=HORIZON_DAYS, initial=0),
            pq_index={},
            pinned_orders={},
            base_date=base_date,
        )

    def to_json(self) -> str:
        """Serialize to a JSON string (suitable for Redis).

        The pq is serialized as a list of orders (insertion order from the
        dict). ``from_json`` doesn't need a particular order to reconstruct
        correctly — the canonical EDF ordering is derived at iteration
        time via ``_iter_pq_edf_sorted``.
        """
        return json.dumps(
            {
                "capacity_values": self.capacity_tree.to_array(),
                "deadline_values": self.deadline_tree.to_array(),
                "priority_queue": [o.model_dump(mode="json") for o in self.pq_index.values()],
                "pinned_orders": [p.model_dump(mode="json") for p in self.pinned_orders.values()],
                "base_date": self.base_date.isoformat(),
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> SchedulerState:
        """Reconstruct from a JSON string produced by ``to_json``.

        ``pinned_orders`` defaults to ``[]`` if absent — keeps the loader
        backward-compatible with state blobs persisted before the pin
        feature shipped. The ``priority_queue`` JSON list (legacy field
        name preserved for wire compatibility) is loaded into ``pq_index``.
        """
        data = json.loads(raw)
        pq_orders = [SchedulingOrder.model_validate(o) for o in data["priority_queue"]]
        pinned = [PinnedOrder.model_validate(p) for p in data.get("pinned_orders", [])]
        return cls(
            capacity_tree=SegmentTree.from_array(data["capacity_values"]),
            deadline_tree=SegmentTree.from_array(data["deadline_values"]),
            pq_index={o.order_id: o for o in pq_orders},
            pinned_orders={p.order_id: p for p in pinned},
            base_date=date.fromisoformat(data["base_date"]),
        )


def _iter_pq_edf_sorted(state: SchedulerState) -> list[SchedulingOrder]:
    """Materialize the pq in EDF order: deadline → -qty → order_number.

    Bucket-sorts by ``deadline_rel`` (30 buckets, one per horizon day),
    then sorts each bucket by ``sort_key`` for the secondary tie-break.

    Complexity: O(n) for the bucket placement pass + O(Σ K_d log K_d) for
    within-day sort. Worst case (all orders same deadline) is O(n log n);
    typical case (uniform spread over D=30 days) is O(n + n log(n/D)),
    materially better than the SortedKeyList-maintained-at-every-mutation
    approach we used before.

    Returns a fresh list — callers may index, slice, or iterate it freely.
    Orders with a deadline outside the horizon (defensive — should be
    filtered upstream) end up in a synthetic 30-day-out bucket so they
    still get a deterministic placement rather than being silently
    dropped here.
    """
    if not state.pq_index:
        return []
    buckets: list[list[SchedulingOrder]] = [[] for _ in range(HORIZON_DAYS + 1)]
    for order in state.pq_index.values():
        rel = abs_to_rel(order.deadline, state.base_date)
        # Out-of-horizon orders shouldn't exist in pq (callers filter at
        # admission), but if they do, park them at the last bucket so
        # iteration still yields them deterministically.
        idx = rel if rel is not None else HORIZON_DAYS
        buckets[idx].append(order)
    flat: list[SchedulingOrder] = []
    for bucket in buckets:
        if len(bucket) > 1:
            bucket.sort(key=lambda o: o.sort_key())
        flat.extend(bucket)
    return flat


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _leftmost_prefix_geq(tree: SegmentTree, target: int, max_idx: int) -> int:
    """Smallest index ``p`` in [1, max_idx] with ``tree.query(p) >= target``.

    The horizon is 30 days, so a linear scan is fast and obviously correct.
    Callers ensure ``tree.query(max_idx) >= target`` so a position exists.
    """
    for i in range(1, max_idx + 1):
        if tree.query(i) >= target:
            return i
    return max_idx  # unreachable when caller's precondition holds


def _pq_add(state: SchedulerState, order: SchedulingOrder) -> None:
    """Insert ``order`` into the pq dict.

    O(1). EDF ordering is derived at iteration time
    (``_iter_pq_edf_sorted``) rather than maintained at every mutation,
    so no sort overhead here.

    Caller is responsible for ensuring the order isn't already present —
    ``add_order`` does this check upfront and returns capacity_exceeded
    on conflict.
    """
    state.pq_index[order.order_id] = order


def _pq_remove_by_id(state: SchedulerState, order_id: uuid.UUID) -> SchedulingOrder | None:
    """Drop ``order_id`` from the pq dict; return the removed order or None.

    O(1). ``None`` means the order wasn't in pq (e.g. already pinned —
    pinned orders live in ``pinned_orders``, not pq).
    """
    return state.pq_index.pop(order_id, None)


def pq_add(state: SchedulerState, order: SchedulingOrder) -> None:
    """Public alias for ``_pq_add`` — pq insertion only, no tree side-effects.

    Used by the batch admission path in the worker: ``apply_batch_to_capacity``
    + ``apply_batch_to_deadline`` apply the tree updates for a whole batch
    of add/remove ops at once, then the worker iterates each accepted
    compound and calls ``pq_add`` per ``add`` leaf to insert the order
    into pq. Decoupling pq mutation from tree mutation is what makes the
    batch path's tree work O(D log D) per accepted prefix instead of
    O(K · log D) per K-op batch.

    The caller is responsible for ensuring the order isn't already in pq
    (the batch feasibility check + producer admission control upstream
    are the guarantees; PATCH-style compounds put remove before add so pq
    is cleared first).
    """
    _pq_add(state, order)


def pq_remove(state: SchedulerState, order_id: uuid.UUID) -> SchedulingOrder | None:
    """Public alias for ``_pq_remove_by_id`` — pq removal only, no tree side-effects.

    Returns the removed order, or ``None`` if it wasn't in pq (e.g. it
    was already pinned, sitting in ``state.pinned_orders`` instead).
    Batch-path callers treat ``None`` as a no-op at the pq level — the
    compound's db_action still runs because the user-facing column write
    is independent of pq membership.
    """
    return _pq_remove_by_id(state, order_id)


def _pq_contains(state: SchedulerState, order_id: uuid.UUID) -> bool:
    """O(1) membership check via the index dict."""
    return order_id in state.pq_index


def _apply_add_to_trees(state: SchedulerState, order: SchedulingOrder) -> None:
    """Apply the tree-only side-effects of an add operation.

    Updates ``deadline_tree`` and backward-fills ``capacity_tree``. Caller
    is responsible for verifying that ``order.deadline`` lies inside the
    horizon and that capacity is sufficient — this helper is intentionally
    silent so it can be reused by ``advance_day`` without re-validating.
    """
    rel = abs_to_rel(order.deadline, state.base_date)
    if rel is None:
        raise ValueError(f"deadline {order.deadline} outside the 30-day horizon")
    state.deadline_tree.point_update(rel, order.wafer_quantity)

    # Backward-fill capacity_tree:
    #   * locate the leftmost day p whose prefix already reaches (b - x);
    #   * zero out (p, rel] (those days are now fully consumed by this order);
    #   * reduce day p so that the prefix sum at rel falls by exactly x.
    x = order.wafer_quantity
    b = state.capacity_tree.query(rel)
    target_prefix = b - x
    upper = _leftmost_prefix_geq(state.capacity_tree, target_prefix, rel)
    a = state.capacity_tree.query(upper)

    if upper + 1 <= rel:
        state.capacity_tree.range_set(upper + 1, rel, 0)
    # delta is non-positive: a >= target_prefix by construction.
    state.capacity_tree.point_update(upper, target_prefix - a)


def _apply_remove_to_trees(state: SchedulerState, order: SchedulingOrder) -> None:
    """Apply the tree-only side-effects of a remove operation.

    Subtracts the order's quantity from ``deadline_tree`` then walks forward
    from the latest tight day, restoring capacity day-by-day. Caller is
    responsible for verifying horizon and pq membership.
    """
    rel = abs_to_rel(order.deadline, state.base_date)
    if rel is None:
        raise ValueError(f"deadline {order.deadline} outside the 30-day horizon")
    state.deadline_tree.point_update(rel, -order.wafer_quantity)

    # Latest day in [0, rel-1] where capacity + deadline obligations reach
    # the cumulative ceiling D*a — i.e., zero slack remains in [1, a].
    tight = 0
    for d in range(rel - 1, 0, -1):
        if state.capacity_tree.query(d) + state.deadline_tree.query(d) == d * DAILY_CAPACITY:
            tight = d
            break

    remaining = order.wafer_quantity
    for d in range(tight + 1, rel + 1):
        # Cumulative slack from (tight, d]; recomputed each iteration so it
        # implicitly reflects additions already made on earlier days.
        slack = d * DAILY_CAPACITY - state.capacity_tree.query(d) - state.deadline_tree.query(d)
        if slack <= 0:
            continue
        give_back = min(slack, remaining)
        state.capacity_tree.point_update(d, give_back)
        remaining -= give_back
        if remaining == 0:
            break

    if remaining > 0:
        # P2-5: residual capacity not given back means the tree state has
        # diverged from the order's obligation. Pre-fix this only logged a
        # warning, but the algorithm continuing on a corrupted state would
        # silently propagate the divergence into compute_schedule and into
        # DB writes. Raising here lets ``_process_compound``'s saga rollback
        # restore the pre-compound snapshot, contains the corruption to the
        # current compound, and surfaces ``schedule.compound_failed`` to the
        # requester so ops can react. Recovery still goes through
        # ``POST /schedule/rebuild`` if the residual indicates a deeper
        # invariant break.
        logger.error(
            "schedule.remove.unexpected_residual",
            order_id=str(order.order_id),
            residual=remaining,
        )
        raise RuntimeError(
            f"remove_order: {remaining} wafers of order {order.order_id} could "
            "not be given back to capacity_tree — segment tree invariant broken; "
            "rolling back compound."
        )


# ---------------------------------------------------------------------------
# add_order
# ---------------------------------------------------------------------------


def add_order(state: SchedulerState, order: SchedulingOrder) -> ScheduleResult:
    """Insert an order into the schedule.

    Returns ``deadline_too_far`` if the deadline lies outside the 30-day
    horizon, ``capacity_exceeded`` if no capacity remains in
    ``[base_date, deadline]``, otherwise ``success`` and mutates ``state``
    in place.
    """
    rel = abs_to_rel(order.deadline, state.base_date)
    if rel is None:
        logger.warning(
            "schedule.add.deadline_too_far",
            order_id=str(order.order_id),
            deadline=order.deadline.isoformat(),
        )
        return ScheduleResult(
            status="deadline_too_far",
            order_id=order.order_id,
            message="Deadline outside the 30-day scheduling horizon.",
        )

    # Membership guard: refuse to add an order that's already in pq OR in
    # pinned_orders. Without this, re-adding the same order_id would double-
    # count its wafers in the segment trees (capacity_tree gets deducted
    # twice, deadline_tree gets incremented twice) and silently corrupt the
    # state until rebuild. Typical trigger: producer sends `add` for an
    # order that was already added (e.g., a duplicate op leaked through, or
    # a PATCH flow that forgot the preceding `remove`).
    if _pq_contains(state, order.order_id):
        logger.warning(
            "schedule.add.already_in_pq",
            order_id=str(order.order_id),
        )
        return ScheduleResult(
            status="capacity_exceeded",
            order_id=order.order_id,
            message="Order is already in the priority queue.",
        )
    if order.order_id in state.pinned_orders:
        logger.warning(
            "schedule.add.already_pinned",
            order_id=str(order.order_id),
        )
        return ScheduleResult(
            status="capacity_exceeded",
            order_id=order.order_id,
            message="Order is already pinned; unpin it before re-adding.",
        )

    available = state.capacity_tree.query(rel)
    if available < order.wafer_quantity:
        logger.warning(
            "schedule.add.capacity_exceeded",
            order_id=str(order.order_id),
            requested=order.wafer_quantity,
            available=available,
        )
        return ScheduleResult(
            status="capacity_exceeded",
            order_id=order.order_id,
            message=(
                f"Need {order.wafer_quantity} wafers, only {available} "
                "available before the deadline."
            ),
        )

    _pq_add(state, order)
    _apply_add_to_trees(state, order)

    logger.info(
        "schedule.add.success",
        order_id=str(order.order_id),
        deadline=order.deadline.isoformat(),
        wafer_quantity=order.wafer_quantity,
    )
    return ScheduleResult(status="success", order_id=order.order_id)


# ---------------------------------------------------------------------------
# remove_order
# ---------------------------------------------------------------------------


def remove_order(state: SchedulerState, order: SchedulingOrder) -> ScheduleResult:
    """Drop an order from the schedule and restore its capacity.

    Caller is responsible for ensuring ``order`` was previously added.
    Capacity is restored by walking forward from the latest "tight" day
    (where every unit in [1, a] is fully committed to deadlines within
    [1, a]) and topping up each subsequent day until ``wafer_quantity``
    units have been returned.
    """
    rel = abs_to_rel(order.deadline, state.base_date)
    if rel is None:
        return ScheduleResult(
            status="deadline_too_far",
            order_id=order.order_id,
            message="Deadline outside the 30-day scheduling horizon.",
        )

    # Membership guard: refuse to remove an order that isn't currently in
    # pq. Without this, ``_apply_remove_to_trees`` would still subtract from
    # the trees as if the order were there → silent capacity corruption.
    # Most realistic trigger: producer sends `remove` for a pinned order
    # without first sending `unpin` (pinned orders live in pinned_orders,
    # not pq). The caller is told via WS so they can correct the flow.
    if not _pq_contains(state, order.order_id):
        is_pinned = order.order_id in state.pinned_orders
        logger.warning(
            "schedule.remove.not_in_pq",
            order_id=str(order.order_id),
            is_pinned=is_pinned,
        )
        return ScheduleResult(
            status="capacity_exceeded",
            order_id=order.order_id,
            message=(
                "Order is currently pinned; send `unpin` before `remove`."
                if is_pinned
                else "Order is not in the priority queue."
            ),
        )

    _pq_remove_by_id(state, order.order_id)
    _apply_remove_to_trees(state, order)

    logger.info(
        "schedule.remove.success",
        order_id=str(order.order_id),
        deadline=order.deadline.isoformat(),
        wafer_quantity=order.wafer_quantity,
    )
    return ScheduleResult(status="success", order_id=order.order_id)


# ---------------------------------------------------------------------------
# pin_order / unpin_order
# ---------------------------------------------------------------------------


def pin_order(
    state: SchedulerState,
    order: SchedulingOrder,
    fake_deadline: date,
) -> ScheduleResult:
    """Lock ``order`` to ``fake_deadline`` (a day ≤ its real deadline).

    Acceptance test mirrors ``add_order``: simulate the swap by removing the
    order's contribution at the real deadline, then asking whether the trees
    can accept it with effective deadline ``fake_deadline``. If yes, commit
    (move from pq to ``pinned_orders``); if no, undo (re-add at real
    deadline) so the caller's failure path is a true no-op against state.

    Returns:
        ``success`` — order moved to pinned list, trees re-indexed at fake.
        ``deadline_too_far`` — fake_deadline outside [base_date, base_date+29].
        ``capacity_exceeded`` — no capacity in [base_date, fake_deadline].

    Caller is responsible for ensuring the order is currently in
    ``pq_index``; if it isn't (e.g. it was already pinned, or never
    added) the function logs and returns ``capacity_exceeded`` so the worker
    has a uniform failure-notify path.
    """
    fake_rel = abs_to_rel(fake_deadline, state.base_date)
    if fake_rel is None:
        logger.warning(
            "schedule.pin.deadline_too_far",
            order_id=str(order.order_id),
            fake_deadline=fake_deadline.isoformat(),
        )
        return ScheduleResult(
            status="deadline_too_far",
            order_id=order.order_id,
            message="Pin date outside the 30-day scheduling horizon.",
        )

    # Order MUST be in pq currently — otherwise we have nothing to remove
    # and the trees would double-count if we still re-added at fake.
    if not _pq_contains(state, order.order_id):
        logger.warning(
            "schedule.pin.order_not_in_pq",
            order_id=str(order.order_id),
        )
        return ScheduleResult(
            status="capacity_exceeded",
            order_id=order.order_id,
            message="Order is not currently in the priority queue.",
        )

    # Step 1: tentatively free the order's tree contribution at real deadline.
    _pq_remove_by_id(state, order.order_id)
    _apply_remove_to_trees(state, order)

    # Step 2: capacity check at fake_deadline (same query as add_order).
    available = state.capacity_tree.query(fake_rel)
    if available < order.wafer_quantity:
        # Undo: re-add at the real deadline so state is bit-for-bit unchanged.
        _apply_add_to_trees(state, order)
        _pq_add(state, order)
        logger.warning(
            "schedule.pin.capacity_exceeded",
            order_id=str(order.order_id),
            requested=order.wafer_quantity,
            available=available,
            fake_deadline=fake_deadline.isoformat(),
        )
        return ScheduleResult(
            status="capacity_exceeded",
            order_id=order.order_id,
            message=(
                f"Need {order.wafer_quantity} wafers on or before "
                f"{fake_deadline.isoformat()}, only {available} available."
            ),
        )

    # Step 3: commit. Tree update uses fake_deadline as the effective
    # deadline; we keep the *real* deadline on the PinnedOrder record so
    # unpin_order can put the order back into pq with the correct deadline.
    pinned_view = SchedulingOrder(
        order_id=order.order_id,
        order_number=order.order_number,
        wafer_quantity=order.wafer_quantity,
        deadline=fake_deadline,
    )
    _apply_add_to_trees(state, pinned_view)
    state.pinned_orders[order.order_id] = PinnedOrder(
        order_id=order.order_id,
        order_number=order.order_number,
        wafer_quantity=order.wafer_quantity,
        deadline=order.deadline,
        fake_deadline=fake_deadline,
    )

    logger.info(
        "schedule.pin.success",
        order_id=str(order.order_id),
        fake_deadline=fake_deadline.isoformat(),
        deadline=order.deadline.isoformat(),
        wafer_quantity=order.wafer_quantity,
    )
    return ScheduleResult(status="success", order_id=order.order_id)


def unpin_order(state: SchedulerState, order_id: uuid.UUID) -> ScheduleResult:
    """Release a pinned order back to the priority queue.

    Looks up the ``PinnedOrder`` by id, removes its tree contribution at
    ``fake_deadline``, then re-adds at the original ``deadline`` and inserts
    into pq. Capacity should always be available (the order's prior pq
    presence was successful), but if for some reason it isn't (edge case:
    base_date advanced past deadline while pinned) the function returns
    ``deadline_too_far`` and leaves the order off the pq with state
    unchanged from the post-remove view.
    """
    # O(1) lookup + remove from the pinned_orders dict.
    target = state.pinned_orders.pop(order_id, None)
    if target is None:
        logger.warning(
            "schedule.unpin.not_pinned",
            order_id=str(order_id),
        )
        return ScheduleResult(
            status="capacity_exceeded",
            order_id=order_id,
            message="Order is not currently pinned.",
        )

    # Treat pinned record as an "add at fake_deadline" for tree accounting,
    # then reverse via _apply_remove_to_trees.
    pinned_view = SchedulingOrder(
        order_id=target.order_id,
        order_number=target.order_number,
        wafer_quantity=target.wafer_quantity,
        deadline=target.fake_deadline,
    )
    _apply_remove_to_trees(state, pinned_view)

    real_view = SchedulingOrder(
        order_id=target.order_id,
        order_number=target.order_number,
        wafer_quantity=target.wafer_quantity,
        deadline=target.deadline,
    )
    real_rel = abs_to_rel(target.deadline, state.base_date)
    if real_rel is None:
        logger.warning(
            "schedule.unpin.deadline_too_far",
            order_id=str(order_id),
            deadline=target.deadline.isoformat(),
        )
        return ScheduleResult(
            status="deadline_too_far",
            order_id=order_id,
            message="Real deadline now outside the 30-day horizon; order dropped.",
        )

    _apply_add_to_trees(state, real_view)
    _pq_add(state, real_view)

    logger.info(
        "schedule.unpin.success",
        order_id=str(order_id),
        deadline=target.deadline.isoformat(),
        wafer_quantity=target.wafer_quantity,
    )
    return ScheduleResult(status="success", order_id=order_id)


# ---------------------------------------------------------------------------
# Batch admission — feasibility check + tree updates for a contiguous prefix
# of the pending queue
# ---------------------------------------------------------------------------
#
# Motivation: per-compound processing in run_scheduling_task pays O(log n * D)
# per op for the segment-tree backward-fill (``_apply_add_to_trees``) plus a
# saga snapshot/rollback per compound (a full ``SchedulerState`` deepcopy for
# every compound, allocator-heavy under bursts). For a queue of N compounds,
# total cost ~= N * (per-compound tree ops + snapshot).
#
# Batch admission instead asks "can the next K compounds in queue order be
# accepted as one unit?" by collapsing all their add/remove ops into a single
# per-day demand array (``compute_batch_capacity_delta``), then comparing its
# prefix sum against ``capacity_tree``'s prefix sum
# (``is_batch_feasible``). If the answer is yes for K=N, accept all of them
# in a single pair of tree updates: ``apply_batch_to_capacity`` (carry-back
# distribution) and ``apply_batch_to_deadline`` (direct point updates). If
# the answer is no, halve and try [1..N/2], [1..N/4], ... until the first
# feasible prefix is found. Worst case is log N halvings * O(D) per check,
# vs the old N tree mutations + N snapshots.
#
# Pin / unpin ops are intentionally excluded from the delta table — their
# capacity impact is a swap between real and fake deadlines that the
# producer's admission control already validated, so the worker treats them
# as structural moves applied after the batch tree update (see the relevant
# section of ``run_scheduling_task``).


class BatchOp(NamedTuple):
    """One delta-affecting op for batch feasibility / tree application.

    The worker translates the JSON leaf-op shape into these tuples before
    handing them to ``compute_batch_capacity_delta`` so the service layer
    never sees the producer's dict format. Pin / unpin ops do not have a
    ``BatchOp`` representation because they are excluded from the delta
    table by design.
    """

    kind: Literal["add", "remove"]
    wafer_quantity: int
    deadline: date


def compute_batch_capacity_delta(
    ops: Iterable[BatchOp],
    base_date: date,
) -> list[int]:
    """Fold a batch of add/remove ops into a per-day net demand array.

    Returns a list of length ``HORIZON_DAYS`` where index ``i`` (0-based)
    holds the net signed quantity for segment-tree day ``rel = i + 1``.

    Sign convention:

    - ``add`` ⇒ ``+wafer_quantity`` at the order's deadline day
    - ``remove`` ⇒ ``-wafer_quantity`` at the order's deadline day

    Ops whose deadline falls outside ``[base_date, base_date + HORIZON_DAYS - 1]``
    are silently dropped — the worker is responsible for catching these
    upstream (an out-of-horizon add cannot be scheduled and should be
    surfaced as ``compound_failed`` before reaching the batch path). PATCH
    compounds where both halves are out-of-horizon would just net to zero
    and contribute nothing here, which is acceptable.

    Complexity: O(K) for K total ops in the batch. No tree queries.
    """
    delta = [0] * HORIZON_DAYS
    for op in ops:
        rel = abs_to_rel(op.deadline, base_date)
        if rel is None:
            continue
        signed = op.wafer_quantity if op.kind == "add" else -op.wafer_quantity
        delta[rel - 1] += signed
    return delta


def is_batch_feasible(state: SchedulerState, delta: list[int]) -> bool:
    """Whether the batch's per-day demand fits in current remaining capacity.

    The batch is feasible iff the demand prefix sum never exceeds the
    capacity prefix sum at any horizon day::

        ∀ i ∈ [1, HORIZON_DAYS]:  Σ_{j ≤ i} delta[j-1]  ≤  capacity_tree.query(i)

    A negative cumulative demand (the batch nets to a removal at day ``i``)
    trivially satisfies the inequality at that day because
    ``capacity_tree.query(i) ≥ 0`` always holds. The check is symmetric in
    sign and does not need a separate path for removal-heavy batches.

    Complexity: O(HORIZON_DAYS · log HORIZON_DAYS) due to per-day tree query.
    """
    if len(delta) != HORIZON_DAYS:
        raise ValueError(
            f"is_batch_feasible: expected delta of length {HORIZON_DAYS}, got {len(delta)}"
        )
    cumulative_demand = 0
    for i, day_demand in enumerate(delta, start=1):
        cumulative_demand += day_demand
        if cumulative_demand > state.capacity_tree.query(i):
            return False
    return True


def apply_batch_to_capacity(state: SchedulerState, delta: list[int]) -> None:
    """Carry-back rewrite of ``capacity_tree`` raw values for an accepted batch.

    Caller MUST have already checked ``is_batch_feasible(state, delta)`` —
    this function does not re-validate and an infeasible delta will leave
    the tree in an inconsistent state (raw values would clamp at 0 / cap
    while the prefix sums no longer match the underlying demand
    commitment).

    Two-branch update walking day ``HORIZON_DAYS`` down to ``1`` while
    maintaining a ``carry`` for demand that did not fit on later days
    (positive carry, demand pushed earlier) or freed capacity that did not
    fit at the deadline day (negative carry, free slots pushed later)::

        net = delta[i] + carry
        if net ≥ 0:                                    # net consumption at day i
            new_rem[i] = max(0, old_rem[i] - net)
            carry      = max(0, net - old_rem[i])
        else:                                          # net restoration at day i
            new_rem[i] = min(DAILY_CAPACITY, old_rem[i] - net)
            carry      = min(0, net + (DAILY_CAPACITY - old_rem[i]))

    Carry semantics: positive value = demand still owed to earlier days
    (EDF spill-back); negative value = freed capacity still owed to later
    days (so daily-cap clamp respected). The clamps to 0 on each branch
    keep carry from drifting across sign — a day with no demand consumes
    or restores nothing and must leave carry exactly where it was rather
    than letting an unclamped subtraction push it across zero. Raw values
    stay in ``[0, DAILY_CAPACITY]`` — the segment tree's per-day invariant
    — while the prefix sum (what feasibility checks look at) shifts by
    exactly ``-Σ delta``.

    The tree is patched in place via ``point_update`` deltas computed
    against ``capacity_tree.to_array()`` (one O(n log n) materialization)
    rather than ``range_set``, so existing query results for unrelated
    days remain cache-warm.

    Complexity: O(HORIZON_DAYS · log HORIZON_DAYS).
    """
    if len(delta) != HORIZON_DAYS:
        raise ValueError(
            f"apply_batch_to_capacity: expected delta of length {HORIZON_DAYS}, "
            f"got {len(delta)}"
        )
    old_rem = state.capacity_tree.to_array()
    new_rem = [0] * HORIZON_DAYS
    carry = 0
    for i in range(HORIZON_DAYS - 1, -1, -1):
        day_old = old_rem[i]
        net = delta[i] + carry
        if net >= 0:
            new_rem[i] = max(0, day_old - net)
            carry = max(0, net - day_old)
        else:
            new_rem[i] = min(DAILY_CAPACITY, day_old - net)
            carry = min(0, net + (DAILY_CAPACITY - day_old))
    for i in range(HORIZON_DAYS):
        diff = new_rem[i] - old_rem[i]
        if diff != 0:
            state.capacity_tree.point_update(i + 1, diff)


def apply_batch_to_deadline(state: SchedulerState, delta: list[int]) -> None:
    """Apply the per-day delta straight onto ``deadline_tree``.

    ``deadline_tree`` tracks the sum of orders due on each day, so add /
    remove ops at day ``rel = i + 1`` map 1:1 to a signed point update.
    No carry-back needed — ``deadline_tree`` raw values are unbounded (a
    day can hold any number of orders) and EDF distribution does not
    apply because deadline is a fixed attribute of each order rather than
    a placement decision.

    Complexity: O(D · log D) for D non-zero days in the delta.
    """
    if len(delta) != HORIZON_DAYS:
        raise ValueError(
            f"apply_batch_to_deadline: expected delta of length {HORIZON_DAYS}, "
            f"got {len(delta)}"
        )
    for i, day_delta in enumerate(delta, start=1):
        if day_delta != 0:
            state.deadline_tree.point_update(i, day_delta)


# ---------------------------------------------------------------------------
# compute_schedule
# ---------------------------------------------------------------------------


def compute_schedule(state: SchedulerState) -> list[ScheduledResult]:
    """Materialize per-order, per-day assignments via greedy forward fill.

    Two-phase fill:

    1. **Pinned orders go first.** Each pinned order's full ``wafer_quantity``
       is placed at its ``fake_deadline`` (no spreading) and that day's
       remaining capacity is decremented accordingly. ``pin_order`` already
       guaranteed capacity exists — if it didn't, that's a bug-shaped state
       inconsistency, not user input, so we fail loudly via assertion.
    2. **Priority queue fills the rest** day-by-day in EDF order against
       the *post-pin* daily_remaining array. Pq orders use their real
       deadline as the latest fillable day, so an order may legitimately
       end up after a pin even though its deadline is earlier — the pin's
       day was reserved before it was the pq's turn.

    Output is purely derived — ``state`` is not mutated.
    """
    daily_remaining = [DAILY_CAPACITY] * (HORIZON_DAYS + 1)  # 1-indexed
    results: list[ScheduledResult] = []

    # Phase 1: pinned orders consume their reserved day.
    for pinned in state.pinned_orders.values():
        fake_rel = abs_to_rel(pinned.fake_deadline, state.base_date)
        if fake_rel is None:
            # Pin has been overtaken by base_date; out-of-band cleanup
            # should remove it. Drop from this run's view.
            logger.warning(
                "schedule.compute.pin_overdue",
                order_id=str(pinned.order_id),
                fake_deadline=pinned.fake_deadline.isoformat(),
                base_date=state.base_date.isoformat(),
            )
            continue
        if daily_remaining[fake_rel] < pinned.wafer_quantity:
            # If this trips it means a pinned order's reservation collides
            # with another commitment — the pin / unpin paths should have
            # prevented it, so log loudly but still emit what we can.
            logger.error(
                "schedule.compute.pin_overcommitted",
                order_id=str(pinned.order_id),
                day=fake_rel,
                requested=pinned.wafer_quantity,
                remaining=daily_remaining[fake_rel],
            )
        assigned = min(daily_remaining[fake_rel], pinned.wafer_quantity)
        if assigned > 0:
            results.append(
                ScheduledResult(
                    order_id=pinned.order_id,
                    scheduled_date=rel_to_abs(fake_rel, state.base_date),
                    quantity=assigned,
                )
            )
            daily_remaining[fake_rel] -= assigned

    # Phase 2: forward-fill pq orders against the post-pin remaining.
    # ``_iter_pq_edf_sorted`` does a bucket sort (by deadline_rel) +
    # within-bucket sort (by qty desc, order_number asc) at iteration time
    # — cheaper than maintaining a SortedKeyList at every mutation,
    # because compute_schedule is the only ordered-iteration consumer.
    for order in _iter_pq_edf_sorted(state):
        deadline_rel = abs_to_rel(order.deadline, state.base_date)
        if deadline_rel is None:
            continue  # outside horizon; should have been rejected upstream

        remaining = order.wafer_quantity
        for d in range(1, deadline_rel + 1):
            if remaining == 0:
                break
            if daily_remaining[d] == 0:
                continue
            assigned = min(daily_remaining[d], remaining)
            results.append(
                ScheduledResult(
                    order_id=order.order_id,
                    scheduled_date=rel_to_abs(d, state.base_date),
                    quantity=assigned,
                )
            )
            daily_remaining[d] -= assigned
            remaining -= assigned

    return results


def capacity_prefix_sums(state: SchedulerState) -> list[tuple[date, int]]:
    """Snapshot ``capacity_tree`` as a per-day prefix-sum series.

    Walks the 30-day horizon and returns ``[(absolute_date, prefix_sum)]``
    where each prefix sum is the cumulative remaining wafer capacity from
    ``state.base_date`` up to and including that day. Pure derivation —
    ``state`` is not mutated.

    Used by ``GET /schedule/capacity`` so the dashboard can plot how much
    spare production capacity exists across the upcoming horizon without
    having to inspect individual orders.
    """
    return [
        (rel_to_abs(rel, state.base_date), state.capacity_tree.query(rel))
        for rel in range(1, HORIZON_DAYS + 1)
    ]


# ---------------------------------------------------------------------------
# advance_day
# ---------------------------------------------------------------------------


def advance_day(state: SchedulerState) -> SchedulerState:
    """Roll the horizon forward one day.

    Steps:
      0. **Pinned-today consumption.** Pinned orders whose ``fake_deadline``
         equals today (``rel == 1``) are produced in full today; remove them
         from ``pinned_orders`` and from the trees. Their wafers count
         against the day's 10,000-wafer ceiling, so the pq accumulator
         starts from ``sum(pinned_today)`` rather than 0.
      1. Walk the priority queue, accumulating ``wafer_quantity`` until the
         daily ceiling (10,000) is reached (counting pinned-today first).
         Orders ahead of the boundary are fully completed; the (optional)
         boundary order is partially completed and continues into the next
         day with reduced quantity.
      2. Apply tree updates: ``remove_order``-style for each fully-done
         order, then for the boundary order ``remove_order`` with the
         original quantity followed by ``add_order`` with the reduced one.
      3. Drop fully-done orders from the pq; rewrite the boundary order in
         place with its remaining quantity (preserving its position — the
         spec deliberately does not re-sort).
      4. Shift both trees left by one day. Index 30 (the new last day) is
         reinitialized: capacity = ``DAILY_CAPACITY``, deadline = 0.
      5. ``base_date += 1 day``.

    The input ``state`` is not mutated — tree edits run on a working copy.
    """
    # ----- Working copy so callers' state is not mutated -------------------
    working = SchedulerState(
        capacity_tree=SegmentTree.from_array(state.capacity_tree.to_array()),
        deadline_tree=SegmentTree.from_array(state.deadline_tree.to_array()),
        pq_index={},  # rebuilt below
        pinned_orders={},  # rebuilt below
        base_date=state.base_date,
    )

    # ----- Step 0: pinned-today (fake_deadline == today) ------------------
    pinned_today: list[PinnedOrder] = []
    pinned_remaining: dict[uuid.UUID, PinnedOrder] = {}
    pinned_today_total = 0
    for pinned in state.pinned_orders.values():
        fake_rel = abs_to_rel(pinned.fake_deadline, state.base_date)
        if fake_rel == 1:
            pinned_today.append(pinned)
            pinned_today_total += pinned.wafer_quantity
        else:
            pinned_remaining[pinned.order_id] = pinned

    # Drop pinned-today from trees (they're done today).
    for pinned in pinned_today:
        pinned_view = SchedulingOrder(
            order_id=pinned.order_id,
            order_number=pinned.order_number,
            wafer_quantity=pinned.wafer_quantity,
            deadline=pinned.fake_deadline,
        )
        _apply_remove_to_trees(working, pinned_view)

    # The day's ceiling for the pq accumulator is whatever capacity is left
    # after pinned-today claimed their share. Defensive ``max(0, ...)``
    # because pin / unpin should have prevented over-commit; we'd rather
    # silently skip pq today than crash if state got corrupted.
    pq_ceiling = max(0, DAILY_CAPACITY - pinned_today_total)

    # ----- Step 1: identify completed and boundary pq orders --------------
    # Materialize the pq in EDF order once; iterate to find which orders
    # finish today (cumulative ≤ ceiling) and which one straddles the
    # boundary (gets partially done).
    pq_edf = _iter_pq_edf_sorted(state)

    cumulative = 0
    fully_done_count = 0
    has_boundary = False

    for order in pq_edf:
        if cumulative + order.wafer_quantity <= pq_ceiling:
            cumulative += order.wafer_quantity
            fully_done_count += 1
            if cumulative == pq_ceiling:
                break
        else:
            has_boundary = True
            break

    fully_done_orders: list[SchedulingOrder] = pq_edf[:fully_done_count]
    boundary_order: SchedulingOrder | None = (
        pq_edf[fully_done_count] if has_boundary else None
    )

    # ----- Step 2: tree updates --------------------------------------------
    for done in fully_done_orders:
        _apply_remove_to_trees(working, done)

    # New pq = everything past the fully-done prefix. Sort order doesn't
    # matter for the dict-backed pq_index — EDF order is rebuilt on
    # demand by ``_iter_pq_edf_sorted``.
    carried_orders: list[SchedulingOrder] = pq_edf[fully_done_count:]

    # The boundary order's qty drops to ``new_quantity`` for the remaining
    # days. Its sort_key (deadline, -qty, order_number) shifts: smaller qty
    # → larger -qty → lower priority within the same deadline. We just
    # swap the boundary entry in ``carried_orders`` for the reduced-qty
    # copy — the dict has no order to preserve, and the next
    # ``_iter_pq_edf_sorted`` will place ``new_boundary`` at its new EDF
    # position by inspecting its updated sort_key.
    if boundary_order is not None:
        done_today = pq_ceiling - cumulative
        new_quantity = boundary_order.wafer_quantity - done_today
        _apply_remove_to_trees(working, boundary_order)
        new_boundary = SchedulingOrder(
            order_id=boundary_order.order_id,
            order_number=boundary_order.order_number,
            wafer_quantity=new_quantity,
            deadline=boundary_order.deadline,
        )
        carried_orders = [o for o in carried_orders if o.order_id != boundary_order.order_id]
        carried_orders.append(new_boundary)
        _apply_add_to_trees(working, new_boundary)

    new_pq_index = {o.order_id: o for o in carried_orders}

    # ----- Step 4: shift trees left by one day -----------------------------
    cap_values = working.capacity_tree.to_array()
    dead_values = working.deadline_tree.to_array()
    new_cap_values = [*cap_values[1:], DAILY_CAPACITY]
    new_dead_values = [*dead_values[1:], 0]

    # ----- Step 5: build the new state -------------------------------------
    new_state = SchedulerState(
        capacity_tree=SegmentTree.from_array(new_cap_values),
        deadline_tree=SegmentTree.from_array(new_dead_values),
        pq_index=new_pq_index,
        pinned_orders=pinned_remaining,
        base_date=state.base_date + timedelta(days=1),
    )

    logger.info(
        "schedule.advance_day",
        old_base=state.base_date.isoformat(),
        new_base=new_state.base_date.isoformat(),
        completed=fully_done_count + (1 if has_boundary else 0),
        pinned_today_done=len(pinned_today),
        carried=len(new_state.pq_index),
        pinned_carried=len(new_state.pinned_orders),
    )
    return new_state


# ---------------------------------------------------------------------------
# rebuild_state
# ---------------------------------------------------------------------------


def rebuild_state(
    orders: list[SchedulingOrder], base_date: date
) -> tuple[SchedulerState, list[SkippedOrder]]:
    """Reset state and re-add all scheduled orders sorted by deadline.

    Use for error recovery and post-migration re-sync. Produces the same
    segment trees and priority queue that a sequence of ``add_order`` /
    ``pin_order`` calls from a clean slate would yield, so the result is
    internally consistent regardless of the previous Redis state.

    Pinned orders (``order.pinned_production_date is not None``) are
    committed first via ``pin_order`` semantics so their fake-deadline-day
    capacity is reserved before pq orders compete for it. Within each
    group orders are sorted by ``sort_key()`` so EDF ordering is
    deterministic.

    Returns ``(new_state, skipped)``. ``skipped`` lists every order that
    ``add_order`` / ``pin_order`` rejected (with reason); the caller is
    expected to surface these to the original requester via WebSocket so
    they can take action. The returned state contains only the orders that
    placed successfully.
    """
    state = SchedulerState.initial(base_date)
    skipped: list[SkippedOrder] = []

    sorted_orders = sorted(orders, key=lambda o: o.sort_key())
    pinned_specs = [o for o in sorted_orders if o.pinned_production_date is not None]
    plain_specs = [o for o in sorted_orders if o.pinned_production_date is None]

    # Pinned orders: insert into pq first, then immediately call pin_order
    # so the trees reflect the fake-deadline reservation. Going through the
    # pq -> pin path (instead of writing trees directly) reuses the same
    # capacity check the live workflow uses, so an over-committed snapshot
    # produces the same skip reasons here.
    for order in pinned_specs:
        if order.pinned_production_date is None:
            # Defensive: ``pinned_specs`` filters by this not being None,
            # but appease the type-checker without using ``assert`` (which
            # ruff S101 disallows in production code).
            continue
        plain_view = SchedulingOrder(
            order_id=order.order_id,
            order_number=order.order_number,
            wafer_quantity=order.wafer_quantity,
            deadline=order.deadline,
        )
        add_result = add_order(state, plain_view)
        if add_result.status != "success":
            skipped.append(
                SkippedOrder(
                    order_id=order.order_id,
                    order_number=order.order_number,
                    reason=add_result.status,
                )
            )
            logger.warning(
                "schedule.rebuild.skip",
                order_id=str(order.order_id),
                order_number=order.order_number,
                reason=add_result.status,
                phase="pinned_add",
            )
            continue
        pin_result = pin_order(state, plain_view, order.pinned_production_date)
        if pin_result.status == "success":
            continue
        # add succeeded but pin failed → leave the order in pq as a
        # safe fallback (better to schedule it anywhere within deadline
        # than drop it entirely) and surface the skip so ops can retry
        # the pin manually.
        skipped.append(
            SkippedOrder(
                order_id=order.order_id,
                order_number=order.order_number,
                reason=pin_result.status,
            )
        )
        logger.warning(
            "schedule.rebuild.skip",
            order_id=str(order.order_id),
            order_number=order.order_number,
            reason=pin_result.status,
            phase="pinned_pin",
        )

    # Plain pq orders.
    for order in plain_specs:
        result = add_order(state, order)
        if result.status == "success":
            continue
        skipped.append(
            SkippedOrder(
                order_id=order.order_id,
                order_number=order.order_number,
                reason=result.status,
            )
        )
        logger.warning(
            "schedule.rebuild.skip",
            order_id=str(order.order_id),
            order_number=order.order_number,
            reason=result.status,
            phase="plain_add",
        )

    logger.info(
        "schedule.rebuild.complete",
        total=len(orders),
        skipped=len(skipped),
        pinned=len(state.pinned_orders),
        pq=len(state.pq_index),
        base_date=base_date.isoformat(),
    )
    return state, skipped
