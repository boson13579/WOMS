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
from datetime import date, timedelta
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

__all__ = [
    "DAILY_CAPACITY",
    "HORIZON_DAYS",
    "PENDING_OPS_KEY",
    "PENDING_OPS_SEQ_KEY",
    "STATE_KEY",
    "STATUS_KEY",
    "ScheduleResult",
    "ScheduledResult",
    "SchedulerState",
    "SchedulingOrder",
    "SegmentTree",
    "SkippedOrder",
    "abs_to_rel",
    "add_order",
    "advance_day",
    "compute_schedule",
    "rebuild_state",
    "rel_to_abs",
    "remove_order",
    "score_for_op",
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
    """Order data needed by the scheduler — decoupled from the SQLAlchemy entity."""

    order_id: uuid.UUID
    order_number: str
    wafer_quantity: int = Field(gt=0)
    deadline: date

    def sort_key(self) -> tuple[date, int, str]:
        """Priority key: deadline early > wafer_quantity large > order_number lex."""
        return (self.deadline, -self.wafer_quantity, self.order_number)


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
    """Live scheduler state; persisted to Redis as JSON between runs."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    capacity_tree: SegmentTree
    deadline_tree: SegmentTree
    priority_queue: list[SchedulingOrder]
    base_date: date

    @classmethod
    def initial(cls, base_date: date) -> SchedulerState:
        """Empty state with full daily capacity and no scheduled orders."""
        return cls(
            capacity_tree=SegmentTree(n=HORIZON_DAYS, initial=DAILY_CAPACITY),
            deadline_tree=SegmentTree(n=HORIZON_DAYS, initial=0),
            priority_queue=[],
            base_date=base_date,
        )

    def to_json(self) -> str:
        """Serialize to a JSON string (suitable for Redis)."""
        return json.dumps(
            {
                "capacity_values": self.capacity_tree.to_array(),
                "deadline_values": self.deadline_tree.to_array(),
                "priority_queue": [o.model_dump(mode="json") for o in self.priority_queue],
                "base_date": self.base_date.isoformat(),
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> SchedulerState:
        """Reconstruct from a JSON string produced by ``to_json``."""
        data = json.loads(raw)
        return cls(
            capacity_tree=SegmentTree.from_array(data["capacity_values"]),
            deadline_tree=SegmentTree.from_array(data["deadline_values"]),
            priority_queue=[SchedulingOrder.model_validate(o) for o in data["priority_queue"]],
            base_date=date.fromisoformat(data["base_date"]),
        )


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


def _insert_sorted(pq: list[SchedulingOrder], order: SchedulingOrder) -> None:
    """Insert ``order`` into ``pq`` while keeping it sorted by priority."""
    pq.append(order)
    pq.sort(key=lambda o: o.sort_key())


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
        logger.warning(
            "schedule.remove.unexpected_residual",
            order_id=str(order.order_id),
            residual=remaining,
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

    _insert_sorted(state.priority_queue, order)
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

    state.priority_queue[:] = [o for o in state.priority_queue if o.order_id != order.order_id]
    _apply_remove_to_trees(state, order)

    logger.info(
        "schedule.remove.success",
        order_id=str(order.order_id),
        deadline=order.deadline.isoformat(),
        wafer_quantity=order.wafer_quantity,
    )
    return ScheduleResult(status="success", order_id=order.order_id)


# ---------------------------------------------------------------------------
# compute_schedule
# ---------------------------------------------------------------------------


def compute_schedule(state: SchedulerState) -> list[ScheduledResult]:
    """Materialize per-order, per-day assignments via greedy forward fill.

    Walks the priority queue in order and, for each order, fills the earliest
    days with available capacity up to the order's deadline. Output is purely
    derived — ``state`` is not mutated.
    """
    daily_remaining = [DAILY_CAPACITY] * (HORIZON_DAYS + 1)  # 1-indexed
    results: list[ScheduledResult] = []

    for order in state.priority_queue:
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


# ---------------------------------------------------------------------------
# advance_day
# ---------------------------------------------------------------------------


def advance_day(state: SchedulerState) -> SchedulerState:
    """Roll the horizon forward one day.

    Steps:
      1. Walk the priority queue, accumulating ``wafer_quantity`` until the
         daily ceiling (10,000) is reached. Orders ahead of the boundary are
         fully completed; the (optional) boundary order is partially
         completed and continues into the next day with reduced quantity.
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
    # ----- Step 1: identify completed and boundary orders ------------------
    cumulative = 0
    fully_done_count = 0
    has_boundary = False

    for order in state.priority_queue:
        if cumulative + order.wafer_quantity <= DAILY_CAPACITY:
            cumulative += order.wafer_quantity
            fully_done_count += 1
            if cumulative == DAILY_CAPACITY:
                break
        else:
            has_boundary = True
            break

    fully_done_orders: list[SchedulingOrder] = list(state.priority_queue[:fully_done_count])
    boundary_order: SchedulingOrder | None = (
        state.priority_queue[fully_done_count] if has_boundary else None
    )

    # ----- Working copy so callers' state is not mutated -------------------
    working = SchedulerState(
        capacity_tree=SegmentTree.from_array(state.capacity_tree.to_array()),
        deadline_tree=SegmentTree.from_array(state.deadline_tree.to_array()),
        priority_queue=[],  # helpers don't read this; pq is rebuilt below
        base_date=state.base_date,
    )

    # ----- Step 2: tree updates --------------------------------------------
    for done in fully_done_orders:
        _apply_remove_to_trees(working, done)

    # New pq starts as everything past the fully-done prefix; the boundary
    # order (if any) is at index 0 and gets rewritten in place.
    new_pq: list[SchedulingOrder] = list(state.priority_queue[fully_done_count:])

    if boundary_order is not None:
        done_today = DAILY_CAPACITY - cumulative
        new_quantity = boundary_order.wafer_quantity - done_today
        _apply_remove_to_trees(working, boundary_order)
        new_boundary = SchedulingOrder(
            order_id=boundary_order.order_id,
            order_number=boundary_order.order_number,
            wafer_quantity=new_quantity,
            deadline=boundary_order.deadline,
        )
        new_pq[0] = new_boundary  # preserve position; do not re-sort
        _apply_add_to_trees(working, new_boundary)

    # ----- Step 4: shift trees left by one day -----------------------------
    cap_values = working.capacity_tree.to_array()
    dead_values = working.deadline_tree.to_array()
    new_cap_values = [*cap_values[1:], DAILY_CAPACITY]
    new_dead_values = [*dead_values[1:], 0]

    # ----- Step 5: build the new state -------------------------------------
    new_state = SchedulerState(
        capacity_tree=SegmentTree.from_array(new_cap_values),
        deadline_tree=SegmentTree.from_array(new_dead_values),
        priority_queue=new_pq,
        base_date=state.base_date + timedelta(days=1),
    )

    logger.info(
        "schedule.advance_day",
        old_base=state.base_date.isoformat(),
        new_base=new_state.base_date.isoformat(),
        completed=fully_done_count + (1 if has_boundary else 0),
        carried=len(new_state.priority_queue),
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
    segment trees and priority queue that a sequence of ``add_order`` calls
    from a clean slate would yield, so the result is internally consistent
    regardless of the previous Redis state.

    Returns ``(new_state, skipped)``. ``skipped`` lists every order that
    ``add_order`` rejected (with reason); the caller is expected to surface
    these to the original requester via WebSocket so they can take action.
    The returned state contains only the orders that placed successfully.
    """
    state = SchedulerState.initial(base_date)
    skipped: list[SkippedOrder] = []
    for order in sorted(orders, key=lambda o: o.sort_key()):
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
        )
    logger.info(
        "schedule.rebuild.complete",
        total=len(orders),
        skipped=len(skipped),
        base_date=base_date.isoformat(),
    )
    return state, skipped
