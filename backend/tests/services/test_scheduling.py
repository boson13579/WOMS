"""Pure-algorithm tests for ``app.services.scheduling``.

No DB, no Redis, no FastAPI — these exercise the segment-tree, EDF queue,
and ``advance_day`` rollover logic against fabricated states.

Run with ``uv run pytest tests/services/test_scheduling.py``.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.services.scheduling import (
    DAILY_CAPACITY,
    HORIZON_DAYS,
    SchedulerState,
    SchedulingOrder,
    abs_to_rel,
    add_order,
    advance_day,
    compute_schedule,
    rebuild_state,
    rel_to_abs,
    remove_order,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = date(2026, 5, 5)


def _make_order(
    *,
    order_number: str = "ORD-X",
    qty: int = 1000,
    deadline: date,
) -> SchedulingOrder:
    return SchedulingOrder(
        order_id=uuid.uuid4(),
        order_number=order_number,
        wafer_quantity=qty,
        deadline=deadline,
    )


# ---------------------------------------------------------------------------
# Date conversion
# ---------------------------------------------------------------------------


def test_abs_to_rel_and_rel_to_abs_roundtrip() -> None:
    for delta in range(HORIZON_DAYS):
        d = _BASE + timedelta(days=delta)
        rel = abs_to_rel(d, _BASE)
        assert rel == delta + 1
        assert rel_to_abs(rel, _BASE) == d


def test_abs_to_rel_outside_horizon_returns_none() -> None:
    assert abs_to_rel(_BASE - timedelta(days=1), _BASE) is None
    assert abs_to_rel(_BASE + timedelta(days=HORIZON_DAYS), _BASE) is None
    # Last day inside the horizon is still valid.
    assert abs_to_rel(_BASE + timedelta(days=HORIZON_DAYS - 1), _BASE) == HORIZON_DAYS


# ---------------------------------------------------------------------------
# add_order
# ---------------------------------------------------------------------------


def test_add_order_success_updates_both_trees() -> None:
    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=2000, deadline=_BASE + timedelta(days=2))  # rel = 3

    result = add_order(state, order)

    assert result.status == "success"
    assert result.order_id == order.order_id
    assert order in state.priority_queue
    # capacity_tree: 30 days * 10000 - 2000 consumed
    assert state.capacity_tree.query(3) == 3 * DAILY_CAPACITY - 2000
    assert state.capacity_tree.query(HORIZON_DAYS) == HORIZON_DAYS * DAILY_CAPACITY - 2000
    # deadline_tree carries the order's quantity at its deadline index
    assert state.deadline_tree.query(3) == 2000


def test_add_order_capacity_exceeded() -> None:
    state = SchedulerState.initial(_BASE)
    # Day 1 has only 10,000 capacity; ask for 20,000 by deadline = today.
    order = _make_order(qty=20_000, deadline=_BASE)

    result = add_order(state, order)

    assert result.status == "capacity_exceeded"
    assert order not in state.priority_queue
    # Trees untouched
    assert state.capacity_tree.query(1) == DAILY_CAPACITY
    assert state.deadline_tree.query(1) == 0


def test_add_order_deadline_too_far() -> None:
    state = SchedulerState.initial(_BASE)
    order = _make_order(
        qty=1000,
        deadline=_BASE + timedelta(days=HORIZON_DAYS),  # one day past the horizon
    )

    result = add_order(state, order)

    assert result.status == "deadline_too_far"
    assert order not in state.priority_queue
    assert state.deadline_tree.query(HORIZON_DAYS) == 0


# ---------------------------------------------------------------------------
# remove_order — round-trip + interaction with other orders
# ---------------------------------------------------------------------------


def test_remove_order_restores_capacity_after_single_add() -> None:
    """Add then remove the same order — state must be indistinguishable from fresh."""
    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=15_000, deadline=_BASE + timedelta(days=2))  # rel = 3

    add_order(state, order)
    assert state.capacity_tree.query(3) == 3 * DAILY_CAPACITY - 15_000

    remove_order(state, order)

    assert state.priority_queue == []
    # Every prefix sum back to the original full-capacity state
    for d in range(1, HORIZON_DAYS + 1):
        assert state.capacity_tree.query(d) == d * DAILY_CAPACITY
        assert state.deadline_tree.query(d) == 0


def test_remove_order_leaves_other_orders_intact() -> None:
    """Doc example abc on day 1 — remove the middle one and verify what's left."""
    state = SchedulerState.initial(_BASE)
    a = _make_order(order_number="a", qty=2000, deadline=_BASE)
    b = _make_order(order_number="b", qty=2000, deadline=_BASE)
    c = _make_order(order_number="c", qty=2000, deadline=_BASE)
    for o in (a, b, c):
        assert add_order(state, o).status == "success"

    # Sanity: capacity at day 1 = 10000 - 6000 = 4000
    assert state.capacity_tree.query(1) == 4000
    assert state.deadline_tree.query(1) == 6000

    remove_order(state, b)

    # Only a + c remain → 4000 used, 6000 free at day 1
    assert state.capacity_tree.query(1) == 6000
    assert state.deadline_tree.query(1) == 4000
    pq_ids = {o.order_id for o in state.priority_queue}
    assert pq_ids == {a.order_id, c.order_id}


def test_remove_order_restores_when_later_add_overlaps_earlier_one() -> None:
    """Regression test for a multi-add overlap case.

    Scenario:
      1. Add order_first (qty=10_000, deadline=base+1, rel=2). Backward-fill
         lands all 10_000 on day 2 → cap day values [10_000, 0, 10_000, ...].
      2. Add order_second (qty=15_000, deadline=base+2, rel=3). Backward-fill
         re-zeroes day 2 (already 0) and day 3, then point-updates day 1 by
         -5_000 → cap day values [5_000, 0, 0, 10_000, ...].
      3. Remove order_second. Trees must roll back to the post-step-1 state
         exactly (day1=10_000, day2=0, day3=10_000), NOT to the naive
         "split 15_000 evenly" result (e.g., day1=10_000, day2=5_000,
         day3=5_000).

    The danger this guards against: an implementation of ``remove_order``
    that computes per-day slacks once before the give-back loop and applies
    them blindly. Such an implementation would over-credit day 2 with 5_000
    that actually belongs to ``order_first``'s deadline obligation. The
    correct algorithm recomputes slack each iteration so that the give-back
    on day 1 is reflected in subsequent days' slack calculations.
    """
    state = SchedulerState.initial(_BASE)
    first = _make_order(order_number="first", qty=10_000, deadline=_BASE + timedelta(days=1))
    second = _make_order(order_number="second", qty=15_000, deadline=_BASE + timedelta(days=2))

    assert add_order(state, first).status == "success"
    # Sanity: capacity prefix matches the doc trace for step 1.
    assert state.capacity_tree.query(1) == 10_000
    assert state.capacity_tree.query(2) == 10_000
    assert state.capacity_tree.query(3) == 20_000

    assert add_order(state, second).status == "success"
    # Sanity: capacity prefix matches the doc trace for step 2.
    assert state.capacity_tree.query(1) == 5_000
    assert state.capacity_tree.query(2) == 5_000
    assert state.capacity_tree.query(3) == 5_000
    assert state.capacity_tree.query(4) == 15_000

    remove_order(state, second)

    # After remove, state must equal "after first only" — day 2 stays at 0
    # (still owned by first's deadline obligation), days 1 and 3 are
    # restored to full capacity, and the deadline tree only carries first.
    assert state.capacity_tree.query(1) == 10_000
    assert state.capacity_tree.query(2) == 10_000
    assert state.capacity_tree.query(3) == 20_000
    for d in range(4, HORIZON_DAYS + 1):
        assert state.capacity_tree.query(d) == d * DAILY_CAPACITY - 10_000

    # deadline_tree: only first's 10_000 obligation at rel=2 remains
    assert state.deadline_tree.query(1) == 0
    assert state.deadline_tree.query(2) == 10_000
    for d in range(3, HORIZON_DAYS + 1):
        assert state.deadline_tree.query(d) == 10_000

    # priority_queue: only first remains
    assert len(state.priority_queue) == 1
    assert state.priority_queue[0].order_id == first.order_id


# ---------------------------------------------------------------------------
# compute_schedule — split across days
# ---------------------------------------------------------------------------


def test_compute_schedule_splits_orders_across_days() -> None:
    state = SchedulerState.initial(_BASE)
    a = _make_order(order_number="a", qty=15_000, deadline=_BASE + timedelta(days=1))
    b = _make_order(order_number="b", qty=8_000, deadline=_BASE + timedelta(days=2))
    c = _make_order(order_number="c", qty=2_000, deadline=_BASE + timedelta(days=2))

    for o in (a, b, c):
        assert add_order(state, o).status == "success"

    results = compute_schedule(state)

    by_order: dict[uuid.UUID, dict[date, int]] = {}
    for r in results:
        by_order.setdefault(r.order_id, {})[r.scheduled_date] = r.quantity

    # a: 10,000 on day 1, 5,000 on day 2
    assert by_order[a.order_id] == {
        _BASE: 10_000,
        _BASE + timedelta(days=1): 5_000,
    }
    # b: 5,000 on day 2 (after a), 3,000 on day 3
    assert by_order[b.order_id] == {
        _BASE + timedelta(days=1): 5_000,
        _BASE + timedelta(days=2): 3_000,
    }
    # c: 2,000 on day 3
    assert by_order[c.order_id] == {_BASE + timedelta(days=2): 2_000}


# ---------------------------------------------------------------------------
# advance_day — full doc example (abc / de / fg with daily-cap boundary on f)
# ---------------------------------------------------------------------------


def test_advance_day_processes_pq_and_shifts_trees() -> None:
    state = SchedulerState.initial(_BASE)
    a = _make_order(order_number="a", qty=2000, deadline=_BASE)
    b = _make_order(order_number="b", qty=2000, deadline=_BASE)
    c = _make_order(order_number="c", qty=2000, deadline=_BASE)
    d = _make_order(order_number="d", qty=1000, deadline=_BASE + timedelta(days=1))
    e = _make_order(order_number="e", qty=2000, deadline=_BASE + timedelta(days=1))
    f = _make_order(order_number="f", qty=2000, deadline=_BASE + timedelta(days=2))
    g = _make_order(order_number="g", qty=2000, deadline=_BASE + timedelta(days=2))

    for o in (a, b, c, d, e, f, g):
        assert add_order(state, o).status == "success"

    # Sanity: matches the doc's "原始 capacity 前綴和: 4000 11000 17000".
    assert state.capacity_tree.query(1) == 4000
    assert state.capacity_tree.query(2) == 11_000
    assert state.capacity_tree.query(3) == 17_000

    new_state = advance_day(state)

    # base_date moved forward exactly one day
    assert new_state.base_date == _BASE + timedelta(days=1)

    # Only the boundary order f (reduced) and the unprocessed g remain.
    assert len(new_state.priority_queue) == 2
    surviving = {o.order_id: o for o in new_state.priority_queue}
    assert set(surviving.keys()) == {f.order_id, g.order_id}

    # f's quantity was 2000; 1000 ran on day 1; remaining = 1000.
    assert surviving[f.order_id].wafer_quantity == 1000
    # g was untouched.
    assert surviving[g.order_id].wafer_quantity == 2000
    # f keeps its position (was directly after the fully-done prefix).
    assert new_state.priority_queue[0].order_id == f.order_id
    assert new_state.priority_queue[1].order_id == g.order_id

    # Capacity prefix after the doc's "步驟 4": 10000, 17000 for the first two
    # days of the new horizon (third day onwards is fresh full-capacity slots).
    assert new_state.capacity_tree.query(1) == 10_000
    assert new_state.capacity_tree.query(2) == 17_000

    # New deadline_tree: day 1 (old day 2, de) is empty; day 2 (old day 3,
    # fg) totals 1000 (f' new qty) + 2000 (g) = 3000.
    assert new_state.deadline_tree.query(1) == 0
    assert new_state.deadline_tree.query(2) == 3000

    # Original state untouched.
    assert state.base_date == _BASE
    assert len(state.priority_queue) == 7


# ---------------------------------------------------------------------------
# rebuild_state — recovery from clean slate
# ---------------------------------------------------------------------------


def test_rebuild_state_empty_orders_returns_empty_state() -> None:
    state, skipped = rebuild_state([], _BASE)

    assert state.base_date == _BASE
    assert state.priority_queue == []
    assert skipped == []
    # Full capacity on every day, no deadline obligations.
    for d in range(1, HORIZON_DAYS + 1):
        assert state.capacity_tree.query(d) == d * DAILY_CAPACITY
        assert state.deadline_tree.query(d) == 0


def test_rebuild_state_single_order_matches_fresh_add() -> None:
    order = _make_order(qty=3000, deadline=_BASE + timedelta(days=4))

    fresh = SchedulerState.initial(_BASE)
    add_order(fresh, order)

    rebuilt, skipped = rebuild_state([order], _BASE)

    assert skipped == []
    assert len(rebuilt.priority_queue) == 1
    assert rebuilt.priority_queue[0].order_id == order.order_id
    # Trees must match a single fresh add_order call exactly.
    for d in range(1, HORIZON_DAYS + 1):
        assert rebuilt.capacity_tree.query(d) == fresh.capacity_tree.query(d)
        assert rebuilt.deadline_tree.query(d) == fresh.deadline_tree.query(d)


def test_rebuild_state_multiple_orders_adds_in_priority_order() -> None:
    # a: later deadline; b: earlier deadline — rebuild should sort b first.
    a = _make_order(order_number="a", qty=2000, deadline=_BASE + timedelta(days=3))
    b = _make_order(order_number="b", qty=5000, deadline=_BASE + timedelta(days=1))

    fresh = SchedulerState.initial(_BASE)
    add_order(fresh, b)
    add_order(fresh, a)

    rebuilt, skipped = rebuild_state([a, b], _BASE)  # intentionally pass a first

    assert skipped == []
    # PQ should have b before a (earlier deadline wins).
    assert rebuilt.priority_queue[0].order_id == b.order_id
    assert rebuilt.priority_queue[1].order_id == a.order_id
    # Tree state should be identical to the correctly-ordered fresh sequence.
    for d in range(1, HORIZON_DAYS + 1):
        assert rebuilt.capacity_tree.query(d) == fresh.capacity_tree.query(d)
        assert rebuilt.deadline_tree.query(d) == fresh.deadline_tree.query(d)


def test_rebuild_state_skips_orders_past_horizon() -> None:
    """Orders whose deadline has fallen outside the 30-day horizon (e.g. an
    order that was scheduled long ago and has been overtaken by ``base_date``
    advancing) must be reported as skipped with the correct reason so the
    caller can notify the original requester."""
    inside = _make_order(order_number="inside", qty=1000, deadline=_BASE + timedelta(days=1))
    outside = _make_order(
        order_number="outside", qty=500, deadline=_BASE + timedelta(days=HORIZON_DAYS)
    )

    state, skipped = rebuild_state([inside, outside], _BASE)

    pq_ids = {o.order_id for o in state.priority_queue}
    assert inside.order_id in pq_ids
    assert outside.order_id not in pq_ids
    # Skipped list carries identity + reason so the caller can notify the
    # original requester via WebSocket.
    assert len(skipped) == 1
    assert skipped[0].order_id == outside.order_id
    assert skipped[0].order_number == "outside"
    assert skipped[0].reason == "deadline_too_far"
