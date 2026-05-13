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
    PinnedOrder,
    SchedulerState,
    SchedulingOrder,
    abs_to_rel,
    add_order,
    advance_day,
    compute_schedule,
    pin_order,
    rebuild_state,
    rel_to_abs,
    remove_order,
    unpin_order,
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
    # PQ data-structure refactor (SortedKeyList): after boundary qty
    # reduction, f's sort_key changes to (day3, -1000, "f"). g still has
    # (day3, -2000, "g"). Because -1000 > -2000, f now sorts AFTER g
    # within the same deadline — g first, then f. This is the EDF-correct
    # ordering; the old per-spec "preserve position" was a deliberate
    # simplification of pre-refactor code and isn't a semantic invariant.
    assert new_state.priority_queue[0].order_id == g.order_id
    assert new_state.priority_queue[1].order_id == f.order_id

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


# ---------------------------------------------------------------------------
# Membership guards (PR-review 第三輪 — 防止重複 add / 對非 pq 訂單 remove)
# ---------------------------------------------------------------------------


def test_add_order_rejects_duplicate_already_in_pq() -> None:
    """Re-adding an order that's already in pq must be rejected, otherwise
    the segment trees would double-count its capacity / deadline contribution
    and silently corrupt state.

    Realistic trigger: producer sends a stale ``add`` op (e.g. retry after
    a partial network failure where the first attempt actually succeeded).
    """
    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=1000, deadline=_BASE + timedelta(days=2))
    first = add_order(state, order)
    assert first.status == "success"

    cap_before = state.capacity_tree.to_array()
    dead_before = state.deadline_tree.to_array()
    pq_len_before = len(state.priority_queue)

    second = add_order(state, order)
    assert second.status == "capacity_exceeded"
    # Critical: state is UNCHANGED by the rejected duplicate.
    assert state.capacity_tree.to_array() == cap_before
    assert state.deadline_tree.to_array() == dead_before
    assert len(state.priority_queue) == pq_len_before


def test_add_order_rejects_when_already_pinned() -> None:
    """Pinned orders live in ``pinned_orders`` (not pq); the trees index
    them at ``fake_deadline``. Allowing add to slip through would add the
    same order's wafers to the trees a second time at the real deadline
    index — total corruption.
    """
    state = SchedulerState.initial(_BASE)
    deadline = _BASE + timedelta(days=4)
    order = _make_order(qty=500, deadline=deadline)
    add_order(state, order)
    pin_order(state, order, fake_deadline=_BASE + timedelta(days=1))

    # Now the order is in pinned_orders, not pq.
    assert not any(o.order_id == order.order_id for o in state.priority_queue)

    second = add_order(state, order)
    assert second.status == "capacity_exceeded"
    assert "pinned" in (second.message or "").lower()


def test_remove_order_rejects_not_in_pq() -> None:
    """``remove_order`` blindly running ``_apply_remove_to_trees`` on an
    order it never added would inject phantom capacity. Most realistic
    trigger: producer sends ``remove`` for a pinned order without
    prepending ``unpin``. The guard surfaces this as a clear failure
    instead of silently corrupting trees.
    """
    state = SchedulerState.initial(_BASE)
    add_order(state, _make_order(order_number="a", qty=1000, deadline=_BASE + timedelta(days=2)))

    cap_before = state.capacity_tree.to_array()
    dead_before = state.deadline_tree.to_array()
    pq_before = list(state.priority_queue)

    phantom = _make_order(order_number="phantom", qty=500, deadline=_BASE + timedelta(days=2))
    result = remove_order(state, phantom)
    assert result.status == "capacity_exceeded"
    # State unchanged.
    assert state.capacity_tree.to_array() == cap_before
    assert state.deadline_tree.to_array() == dead_before
    assert state.priority_queue == pq_before


def test_remove_order_on_pinned_order_gives_pinned_hint() -> None:
    """When the order being remove'd is currently pinned (so it's in
    pinned_orders, not pq), the failure message should hint that the
    producer needs to prepend an ``unpin`` op. This is the single most
    common producer mistake the guard catches in real usage.
    """
    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=500, deadline=_BASE + timedelta(days=4))
    add_order(state, order)
    pin_order(state, order, fake_deadline=_BASE + timedelta(days=1))

    result = remove_order(state, order)
    assert result.status == "capacity_exceeded"
    assert "unpin" in (result.message or "").lower()


# ---------------------------------------------------------------------------
# pin_order / unpin_order
# ---------------------------------------------------------------------------


def test_pin_order_rejected_when_capacity_insufficient_at_pin_day() -> None:
    """Spec example 1: existing (a 9000 dl=1) + (b 2000 dl=2). Pin b to day 1
    must fail because day 1 only has 10000-9000=1000 free, b needs 2000.

    Critically: state must be UNCHANGED after rejection. The pin path
    speculatively removes the order from pq+trees and re-adds at the fake
    day; on capacity failure it has to undo cleanly. Without that undo, a
    rejected pin would silently drop the order.
    """
    state = SchedulerState.initial(_BASE)
    a = _make_order(order_number="a", qty=9000, deadline=_BASE + timedelta(days=0))
    b = _make_order(order_number="b", qty=2000, deadline=_BASE + timedelta(days=1))
    add_order(state, a)
    add_order(state, b)

    # Snapshot trees + pq for post-rejection comparison.
    cap_before = state.capacity_tree.to_array()
    dead_before = state.deadline_tree.to_array()
    pq_ids_before = [o.order_id for o in state.priority_queue]

    result = pin_order(state, b, fake_deadline=_BASE)
    assert result.status == "capacity_exceeded"

    assert state.capacity_tree.to_array() == cap_before
    assert state.deadline_tree.to_array() == dead_before
    assert [o.order_id for o in state.priority_queue] == pq_ids_before
    assert state.pinned_orders == {}


def test_pin_order_success_matches_spec_example_2() -> None:
    """Spec example 2 numbers: (a 9000 dl=3), (b 1000 dl=3), (c 1000 dl=3),
    pin b to day 1 then c to day 1. After both pins:

      * 假deadline 產能前綴和 = [8000, 18000, 19000]
      * deadline 前綴和 = [2000, 2000, 11000]
      * pq holds {a}; pinned_orders holds {b, c}

    These exact numbers come from the user's spec; if the algorithm drifts
    even by a few wafers the rebuild path or compute_schedule will produce
    wrong answers downstream, so we lock the prefix sums explicitly.
    """
    state = SchedulerState.initial(_BASE)
    deadline_3 = _BASE + timedelta(days=2)  # rel = 3
    a = _make_order(order_number="a", qty=9000, deadline=deadline_3)
    b = _make_order(order_number="b", qty=1000, deadline=deadline_3)
    c = _make_order(order_number="c", qty=1000, deadline=deadline_3)
    add_order(state, a)
    add_order(state, b)
    add_order(state, c)

    pin_b = pin_order(state, b, fake_deadline=_BASE)
    assert pin_b.status == "success"
    pin_c = pin_order(state, c, fake_deadline=_BASE)
    assert pin_c.status == "success"

    # Capacity prefix sum exactly matches the spec.
    assert state.capacity_tree.query(1) == 8000
    assert state.capacity_tree.query(2) == 18000
    assert state.capacity_tree.query(3) == 19000
    # Deadline prefix sum: pinned orders' contribution at day 1 (b+c=2000),
    # a's 9000 at day 3.
    assert state.deadline_tree.query(1) == 2000
    assert state.deadline_tree.query(2) == 2000
    assert state.deadline_tree.query(3) == 11000

    pq_ids = {o.order_id for o in state.priority_queue}
    pinned_ids = set(state.pinned_orders.keys())
    assert pq_ids == {a.order_id}
    assert pinned_ids == {b.order_id, c.order_id}


def test_unpin_order_restores_state_to_pre_pin() -> None:
    """Spec example 3: starting from example-2's pinned-{b,c} state, unpin c.
    After unpin: c is back in pq with deadline=3; pinned_orders holds only b.
    Capacity prefix sum should match [9000, 19000, 19000] and deadline prefix
    sum [1000, 1000, 11000] per the spec.
    """
    state = SchedulerState.initial(_BASE)
    deadline_3 = _BASE + timedelta(days=2)
    a = _make_order(order_number="a", qty=9000, deadline=deadline_3)
    b = _make_order(order_number="b", qty=1000, deadline=deadline_3)
    c = _make_order(order_number="c", qty=1000, deadline=deadline_3)
    for o in (a, b, c):
        add_order(state, o)
    pin_order(state, b, fake_deadline=_BASE)
    pin_order(state, c, fake_deadline=_BASE)

    result = unpin_order(state, c.order_id)
    assert result.status == "success"

    assert state.capacity_tree.query(1) == 9000
    assert state.capacity_tree.query(2) == 19000
    assert state.capacity_tree.query(3) == 19000
    assert state.deadline_tree.query(1) == 1000
    assert state.deadline_tree.query(2) == 1000
    assert state.deadline_tree.query(3) == 11000

    pq_ids = {o.order_id for o in state.priority_queue}
    pinned_ids = set(state.pinned_orders.keys())
    assert pq_ids == {a.order_id, c.order_id}
    assert pinned_ids == {b.order_id}


def test_unpin_order_unknown_id_returns_error_without_mutating_state() -> None:
    """Calling unpin on an id not in ``pinned_orders`` is a logic error from
    the producer side; treat as a soft failure so the worker's failure-notify
    path runs, and leave the pq + trees alone.
    """
    state = SchedulerState.initial(_BASE)
    add_order(state, _make_order(order_number="a", qty=1000, deadline=_BASE + timedelta(days=2)))

    cap_before = state.capacity_tree.to_array()
    pq_before = list(state.priority_queue)
    pinned_before = dict(state.pinned_orders)

    result = unpin_order(state, uuid.uuid4())
    assert result.status == "capacity_exceeded"
    assert state.capacity_tree.to_array() == cap_before
    assert list(state.priority_queue) == pq_before
    assert state.pinned_orders == pinned_before


def test_compute_schedule_places_pinned_first_then_fills_pq() -> None:
    """Example 2 daily breakdown — first day produces b1000 + c1000 + a8000;
    second day produces a1000. Validates the two-phase fill: pinned consume
    fake_deadline first, then EDF fills the post-pin remaining.
    """
    state = SchedulerState.initial(_BASE)
    deadline_3 = _BASE + timedelta(days=2)
    a = _make_order(order_number="a", qty=9000, deadline=deadline_3)
    b = _make_order(order_number="b", qty=1000, deadline=deadline_3)
    c = _make_order(order_number="c", qty=1000, deadline=deadline_3)
    for o in (a, b, c):
        add_order(state, o)
    pin_order(state, b, fake_deadline=_BASE)
    pin_order(state, c, fake_deadline=_BASE)

    schedule = compute_schedule(state)

    by_day = {(r.scheduled_date, r.order_id): r.quantity for r in schedule}
    # Day 1: b1000 + c1000 + a8000 (pinned first, then a fills the rest).
    assert by_day[(_BASE, b.order_id)] == 1000
    assert by_day[(_BASE, c.order_id)] == 1000
    assert by_day[(_BASE, a.order_id)] == 8000
    # Day 2: a's remaining 1000.
    assert by_day[(_BASE + timedelta(days=1), a.order_id)] == 1000


def test_advance_day_completes_pinned_today_and_fills_remainder_from_pq() -> None:
    """``fake_deadline == today`` means the pinned order is produced today.

    Setup: pinned x with qty=2000 at day 1, pq order y with qty=15000 dl=day3.
    advance_day's day-1 output should produce 2000(x) + 8000(y) = 10000;
    y carries 7000 wafers into the new pq, and pinned_orders empties.
    Crucial guard: the pq accumulator must use ``DAILY_CAPACITY - pinned_today``
    as its ceiling, not the full DAILY_CAPACITY (would over-produce by 2000).
    """
    state = SchedulerState.initial(_BASE)
    x = _make_order(order_number="x", qty=2000, deadline=_BASE + timedelta(days=2))
    y = _make_order(order_number="y", qty=15000, deadline=_BASE + timedelta(days=2))
    add_order(state, x)
    add_order(state, y)
    pin_order(state, x, fake_deadline=_BASE)

    new_state = advance_day(state)

    # Pinned x is gone — produced today.
    assert new_state.pinned_orders == {}
    # y remains in pq with qty reduced by 8000 (the pq budget after pinned-today).
    assert len(new_state.priority_queue) == 1
    assert new_state.priority_queue[0].order_id == y.order_id
    assert new_state.priority_queue[0].wafer_quantity == 15000 - 8000
    # base_date advanced by 1 day.
    assert new_state.base_date == _BASE + timedelta(days=1)


def test_rebuild_state_separates_pinned_from_pq() -> None:
    """When DB has both pinned and unpinned scheduled orders, rebuild_state
    must put the pinned ones in ``pinned_orders`` (not in pq) and reproduce
    the same trees a live pin would have produced.
    """
    deadline_3 = _BASE + timedelta(days=2)
    a = SchedulingOrder(
        order_id=uuid.uuid4(),
        order_number="a",
        wafer_quantity=9000,
        deadline=deadline_3,
    )
    b = SchedulingOrder(
        order_id=uuid.uuid4(),
        order_number="b",
        wafer_quantity=1000,
        deadline=deadline_3,
        pinned_production_date=_BASE,  # marks this for the pinned path
    )
    state, skipped = rebuild_state([a, b], _BASE)

    assert skipped == []
    pq_ids = {o.order_id for o in state.priority_queue}
    pinned_ids = set(state.pinned_orders.keys())
    assert pq_ids == {a.order_id}
    assert pinned_ids == {b.order_id}
    # Pinned order is recorded with both real + fake deadlines for unpin.
    pinned_b = state.pinned_orders[b.order_id]
    assert pinned_b.deadline == deadline_3
    assert pinned_b.fake_deadline == _BASE


# ---------------------------------------------------------------------------
# Admission control invariants (P1-1)
# ---------------------------------------------------------------------------
#
# Reviewer raised "advance_day with pinned_today_total=DAILY_CAPACITY +
# pq order at dl=today gets stuck in pq forever". The claim is correct
# IF that state is reachable. These tests pin down the invariant: it is
# NOT reachable — ``add_order`` and ``pin_order`` both reject any input
# that would put us there. Future refactors of admission must not break
# these invariants.
#
# Mental model: ``capacity_tree`` is a backward-fill reservation system.
# Adding an order with dl=D reserves wafers in capacity_tree starting
# from day D and walking back. So:
#   - An add for dl=today claims slots on day 1 (`rel=1`).
#   - A pin to day D claims slots on day D.
# ``capacity_tree.query(D)`` is the total remaining (= unreserved) capacity
# across days 1..D. As long as this query returns >= the new order's
# wafer_quantity, the add/pin succeeds and the post-state is feasible.
# When it returns less, admission rejects.


def test_p1_1_invariant_add_after_pin_full_today_rejects() -> None:
    """Pin Y(=DAILY_CAPACITY) to today first; any subsequent add with
    deadline=today must reject because day-1 prefix sum is 0.

    Direction tested: pin first, then add. Confirms the post-pin
    capacity_tree leaves no slack for an EDF-tight pq order.
    """
    state = SchedulerState.initial(_BASE)
    # Y must come in via pq → pin (pin's precondition is "already in pq").
    y = _make_order(order_number="Y", qty=DAILY_CAPACITY, deadline=_BASE)
    assert add_order(state, y).status == "success"
    assert pin_order(state, y, fake_deadline=_BASE).status == "success"
    # Day 1's prefix sum is now 0 (entire DAILY_CAPACITY reserved for Y).
    assert state.capacity_tree.query(1) == 0

    # Now an add with dl=today must be rejected, regardless of qty.
    x = _make_order(order_number="X", qty=1, deadline=_BASE)
    result = add_order(state, x)
    assert result.status == "capacity_exceeded"


def test_p1_1_invariant_pin_full_today_rejects_when_pq_has_today_order() -> None:
    """Add X(dl=today) to pq first, then try to pin some other order Y
    with fake_deadline=today AND Y.wafer_quantity that would exceed day-1
    headroom. The pin must reject because day-1's remaining (after X's
    reservation) is below Y's wafer_quantity.

    Direction tested: add-with-today-deadline first, then pin-to-today.
    This is the path Reviewer's P1-1 scenario implicitly assumed (X
    coexisting with a fully-pinned day-1).
    """
    state = SchedulerState.initial(_BASE)

    x = _make_order(order_number="X", qty=2_000, deadline=_BASE)
    assert add_order(state, x).status == "success"
    # day 1 now has DAILY_CAPACITY - 2_000 remaining.
    assert state.capacity_tree.query(1) == DAILY_CAPACITY - 2_000

    # Y is in pq with a future deadline (so pin_order's "must be in pq"
    # precondition is satisfied). Y's wafer_quantity is large enough that
    # together with X it would over-allocate day 1.
    y = _make_order(
        order_number="Y",
        qty=DAILY_CAPACITY - 1_000,  # X(2000) + Y(9000) = 11000 > DAILY_CAPACITY
        deadline=_BASE + timedelta(days=10),
    )
    assert add_order(state, y).status == "success"

    result = pin_order(state, y, fake_deadline=_BASE)
    assert result.status == "capacity_exceeded"

    # Critical: pin failure must be a true no-op. Y is still in pq with
    # its original deadline, X still in pq with day-1 reservation. The
    # P1-1 stuck state is never reached.
    assert state.capacity_tree.query(1) == DAILY_CAPACITY - 2_000


def test_p1_1_invariant_pin_partial_today_leaves_room_for_existing_pq_order() -> None:
    """If pinning Y leaves *exactly enough* slack for X's deadline-today
    obligation, pin succeeds and the state remains feasible: X's day-1
    portion (its full qty) still fits before its deadline.

    This is the boundary that proves the admission check isn't overly
    conservative — it accepts the maximum legal pin and rejects only the
    next wafer over.
    """
    state = SchedulerState.initial(_BASE)

    x = _make_order(order_number="X", qty=2_000, deadline=_BASE)
    assert add_order(state, x).status == "success"

    y = _make_order(
        order_number="Y",
        # Exact fit: X + Y = DAILY_CAPACITY.
        qty=DAILY_CAPACITY - 2_000,
        deadline=_BASE + timedelta(days=10),
    )
    assert add_order(state, y).status == "success"

    result = pin_order(state, y, fake_deadline=_BASE)
    assert result.status == "success"
    # Day 1 is now fully reserved (no remaining), but the obligations
    # in deadline_tree (X's 2000 + Y's now-pinned 9000) match exactly.
    assert state.capacity_tree.query(1) == 0

    # compute_schedule produces a feasible plan: pinned Y on day 1
    # consumes its 9000 slot; pq's X gets the remaining day-1 1000 slot
    # for its qty=2000? — actually pq_remaining on day 1 = 0 (Y took it
    # all post-pin), but X's pq slot will be on a day where capacity
    # remains. The point of this test is the admission *accepted*, not
    # the materialized schedule shape — leave that to compute_schedule
    # tests.


def test_apply_remove_to_trees_raises_on_residual(monkeypatch) -> None:
    """``_apply_remove_to_trees`` must raise when the forward give-back can't
    distribute the full quantity back to capacity_tree. Pre-fix this only
    logged a warning and let the algorithm continue on a corrupted state,
    silently propagating divergence into compute_schedule + DB writes.
    P2-5: raise instead, so ``_process_compound``'s saga rollback fires
    and the compound surfaces as ``compound_failed`` to the requester.

    The residual path is hard to reach via natural API calls because the
    algorithm normally self-corrects. We construct it by patching
    ``capacity_tree.query`` / ``deadline_tree.query`` to return values
    that fabricate zero slack everywhere — simulating a tree state that
    drifted out of invariant (the exact failure mode the raise is
    defending against).
    """
    import pytest
    from app.services.scheduling import _apply_remove_to_trees

    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=50, deadline=_BASE + timedelta(days=2))

    # Fabricate "tight everywhere, zero slack" by making both trees
    # report the same fully-consumed prefix sum for every day in range.
    monkeypatch.setattr(
        state.capacity_tree,
        "query",
        lambda d: d * DAILY_CAPACITY,
    )
    monkeypatch.setattr(
        state.deadline_tree,
        "query",
        lambda d: 0,
    )
    # point_update on the deadline tree happens before the slack walk;
    # let it no-op so we don't perturb our query() override.
    monkeypatch.setattr(state.deadline_tree, "point_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(state.capacity_tree, "point_update", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="invariant broken"):
        _apply_remove_to_trees(state, order)


def test_scheduler_state_roundtrip_preserves_pinned_orders() -> None:
    """``to_json`` / ``from_json`` must include ``pinned_orders`` so Redis
    persistence survives a worker restart with pins intact. Backward compat
    is also covered: a state blob written before the pin feature shipped
    (i.e. without ``pinned_orders`` key) must deserialize as empty list.
    """
    state = SchedulerState.initial(_BASE)
    seeded = PinnedOrder(
        order_id=uuid.uuid4(),
        order_number="b",
        wafer_quantity=1000,
        deadline=_BASE + timedelta(days=2),
        fake_deadline=_BASE,
    )
    state.pinned_orders[seeded.order_id] = seeded
    raw = state.to_json()
    revived = SchedulerState.from_json(raw)
    assert len(revived.pinned_orders) == 1
    revived_pin = next(iter(revived.pinned_orders.values()))
    assert revived_pin.fake_deadline == _BASE

    # Backward-compat: an old blob without "pinned_orders" key.
    import json as _json

    legacy = _json.loads(raw)
    legacy.pop("pinned_orders")
    legacy_raw = _json.dumps(legacy)
    revived_legacy = SchedulerState.from_json(legacy_raw)
    assert revived_legacy.pinned_orders == {}


# ---------------------------------------------------------------------------
# pin_order / unpin_order failure paths
# ---------------------------------------------------------------------------


def test_pin_order_rejects_fake_deadline_outside_horizon() -> None:
    """Pin with ``fake_deadline`` beyond the 30-day window must return
    ``deadline_too_far`` without mutating state."""
    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=500, deadline=_BASE + timedelta(days=HORIZON_DAYS - 1))
    assert add_order(state, order).status == "success"
    snapshot = state.to_json()

    far_pin_day = _BASE + timedelta(days=HORIZON_DAYS)  # one past the horizon
    result = pin_order(state, order, fake_deadline=far_pin_day)

    assert result.status == "deadline_too_far"
    # State is unchanged — the early-return branch never touched the trees.
    assert state.to_json() == snapshot


def test_pin_order_rejects_order_not_in_pq() -> None:
    """Pinning an order that isn't currently in the pq must return
    ``capacity_exceeded`` (worker-uniform failure status) without touching
    the trees. Most realistic trigger: a duplicated ``pin`` op for an
    already-pinned order, or a ``pin`` for an order that was never
    ``add``-ed."""
    state = SchedulerState.initial(_BASE)
    phantom = _make_order(qty=100, deadline=_BASE + timedelta(days=5))
    snapshot = state.to_json()

    result = pin_order(state, phantom, fake_deadline=_BASE + timedelta(days=3))

    assert result.status == "capacity_exceeded"
    assert state.to_json() == snapshot


def test_unpin_order_drops_when_real_deadline_already_passed() -> None:
    """If the pinned order's *real* deadline has been overtaken by
    ``base_date`` (e.g. it sat pinned across several advance_day rolls
    until its real deadline fell off the back of the horizon), unpin
    can't put it back in the pq. The function returns
    ``deadline_too_far`` after removing the pinned tree contribution —
    the order is dropped from both pq and pinned_orders.
    """
    state = SchedulerState.initial(_BASE)
    pinned = PinnedOrder(
        order_id=uuid.uuid4(),
        order_number="ORD-OVERDUE",
        wafer_quantity=500,
        # Real deadline IS before base_date (rel index would be < 1).
        deadline=_BASE - timedelta(days=1),
        # Fake deadline still inside horizon so the inverse remove-from-
        # trees step doesn't crash.
        fake_deadline=_BASE + timedelta(days=1),
    )
    state.pinned_orders[pinned.order_id] = pinned
    # Reflect the pinned contribution in the trees, matching what
    # ``pin_order`` would have set up.
    pinned_view = SchedulingOrder(
        order_id=pinned.order_id,
        order_number=pinned.order_number,
        wafer_quantity=pinned.wafer_quantity,
        deadline=pinned.fake_deadline,
    )
    from app.services.scheduling import _apply_add_to_trees, unpin_order

    _apply_add_to_trees(state, pinned_view)

    result = unpin_order(state, pinned.order_id)

    assert result.status == "deadline_too_far"
    # Pinned record is gone (pop happened earlier in unpin_order).
    assert pinned.order_id not in state.pinned_orders
    # pq is empty — the order was dropped, not re-added.
    assert pinned.order_id not in {o.order_id for o in state.priority_queue}


# ---------------------------------------------------------------------------
# compute_schedule edge cases
# ---------------------------------------------------------------------------


def test_compute_schedule_skips_pinned_with_overdue_fake_deadline() -> None:
    """If a pinned order's ``fake_deadline`` has been overtaken by
    ``base_date`` (out-of-band scenario; advance_day should have cleaned
    it up first), ``compute_schedule`` logs and skips it rather than
    crashing on a negative index."""
    state = SchedulerState.initial(_BASE)
    overdue = PinnedOrder(
        order_id=uuid.uuid4(),
        order_number="ORD-PIN-OVERDUE",
        wafer_quantity=300,
        deadline=_BASE + timedelta(days=2),
        # In the past — should not happen in production, but be defensive.
        fake_deadline=_BASE - timedelta(days=1),
    )
    state.pinned_orders[overdue.order_id] = overdue

    results = compute_schedule(state)

    # No ScheduledResult emitted for the overdue order.
    assert not any(r.order_id == overdue.order_id for r in results)


def test_compute_schedule_pin_overcommitted_first_wins_full_capacity() -> None:
    """If two pinned orders both reserve more than ``DAILY_CAPACITY`` on
    the same day (admission control should have rejected this, so it
    means upstream state corruption), the first-inserted pin still gets
    its full requested quantity scheduled — over-commit doesn't corrupt
    the winner's slot.
    """
    state = SchedulerState.initial(_BASE)
    pin_day = _BASE + timedelta(days=2)
    p1 = PinnedOrder(
        order_id=uuid.uuid4(),
        order_number="P1",
        wafer_quantity=DAILY_CAPACITY,  # fills the day entirely
        deadline=pin_day,
        fake_deadline=pin_day,
    )
    p2 = PinnedOrder(
        order_id=uuid.uuid4(),
        order_number="P2",
        wafer_quantity=500,  # would overflow if both honored fully
        deadline=pin_day,
        fake_deadline=pin_day,
    )
    state.pinned_orders[p1.order_id] = p1
    state.pinned_orders[p2.order_id] = p2

    results = compute_schedule(state)

    p1_total = sum(r.quantity for r in results if r.order_id == p1.order_id)
    assert p1_total == DAILY_CAPACITY


def test_compute_schedule_pin_overcommitted_dropped_loser_emits_no_rows() -> None:
    """Companion to ``..._first_wins_full_capacity``: the second pinned
    order that doesn't fit gets **dropped entirely** — no ScheduledResult
    rows for it — because the ``assigned > 0`` guard inside the per-day
    loop suppresses zero-quantity emissions.

    Locking this in separately so a future change that removed the
    ``assigned > 0`` guard (and started emitting ``quantity=0`` rows
    instead) would surface here rather than slipping through under the
    old "still_emits" name where the assertion was ``p2_total == 0``.
    """
    state = SchedulerState.initial(_BASE)
    pin_day = _BASE + timedelta(days=2)
    p1 = PinnedOrder(
        order_id=uuid.uuid4(),
        order_number="P1",
        wafer_quantity=DAILY_CAPACITY,
        deadline=pin_day,
        fake_deadline=pin_day,
    )
    p2 = PinnedOrder(
        order_id=uuid.uuid4(),
        order_number="P2",
        wafer_quantity=500,
        deadline=pin_day,
        fake_deadline=pin_day,
    )
    state.pinned_orders[p1.order_id] = p1
    state.pinned_orders[p2.order_id] = p2

    results = compute_schedule(state)

    # No rows at all for p2 — not "one row with quantity=0".
    p2_rows = [r for r in results if r.order_id == p2.order_id]
    assert p2_rows == []


def test_compute_schedule_silently_skips_pq_order_with_invalid_deadline() -> None:
    """pq order with a deadline outside [base_date, base_date + 29] —
    e.g. advance_day overtook a boundary order that never got moved out
    of pq — must NOT crash compute_schedule. Skips silently (already
    logged by advance_day on the way in)."""
    state = SchedulerState.initial(_BASE)
    # Order's deadline is yesterday — abs_to_rel returns None.
    ghost = SchedulingOrder(
        order_id=uuid.uuid4(),
        order_number="GHOST",
        wafer_quantity=100,
        deadline=_BASE - timedelta(days=1),
    )
    # Bypass add_order's guard — directly insert into pq to simulate the
    # state-corruption scenario this branch defends against.
    from app.services.scheduling import _pq_add

    _pq_add(state, ghost)

    results = compute_schedule(state)
    assert not any(r.order_id == ghost.order_id for r in results)


# ---------------------------------------------------------------------------
# rebuild_state failure-mode fallback
# ---------------------------------------------------------------------------


def test_rebuild_state_falls_back_to_pq_when_pin_capacity_exceeded() -> None:
    """During rebuild, if the pinned-phase ``add_order`` succeeds but the
    follow-up ``pin_order`` fails (e.g. the pin day already has another
    pinned order consuming the same capacity), the order stays in pq as
    a safe fallback (better to schedule it within its real deadline
    than drop it) and is surfaced via ``skipped`` so ops can react."""
    # Pin both orders to ``base_date`` (rel=1) so the prefix-sum guard
    # in ``pin_order`` actually bites — for fake_rel=1, capacity_tree's
    # prefix sum is just day-1's capacity, which the first pin can
    # exhaust. For larger fake_rel, the prefix sum spans multiple days
    # and the second pin would still fit.
    pin_day = _BASE
    first = SchedulingOrder(
        order_id=uuid.uuid4(),
        order_number="REBUILD-PIN-A",
        wafer_quantity=DAILY_CAPACITY,  # fills day-1 entirely after pin
        deadline=_BASE + timedelta(days=5),
        pinned_production_date=pin_day,
    )
    second = SchedulingOrder(
        order_id=uuid.uuid4(),
        order_number="REBUILD-PIN-B",
        wafer_quantity=1000,
        deadline=_BASE + timedelta(days=5),
        pinned_production_date=pin_day,
    )

    new_state, skipped = rebuild_state([first, second], _BASE)

    # First pin succeeded → it's in pinned_orders.
    assert first.order_id in new_state.pinned_orders
    # Second order's pin failed but its add succeeded → it stays in pq
    # (not pinned_orders) and shows up in skipped with the pin failure
    # reason.
    pq_ids = {o.order_id for o in new_state.priority_queue}
    assert second.order_id in pq_ids
    assert second.order_id not in new_state.pinned_orders
    skipped_ids = {s.order_id for s in skipped}
    assert second.order_id in skipped_ids


# ---------------------------------------------------------------------------
# SegmentTree boundary guards
# ---------------------------------------------------------------------------


def test_segment_tree_query_raises_index_error_outside_range() -> None:
    """The 1-indexed segment tree must reject out-of-range queries — the
    internal recursion assumes ``1 <= i <= n``. Defensive raise gives a
    clear error instead of silently returning 0 or corrupting the
    recursion's partial sums."""
    import pytest
    from app.services.scheduling import SegmentTree

    tree = SegmentTree(n=HORIZON_DAYS, initial=DAILY_CAPACITY)
    with pytest.raises(IndexError):
        tree.query(0)
    with pytest.raises(IndexError):
        tree.query(HORIZON_DAYS + 1)


def test_segment_tree_range_set_raises_on_invalid_bounds() -> None:
    import pytest
    from app.services.scheduling import SegmentTree

    tree = SegmentTree(n=HORIZON_DAYS, initial=DAILY_CAPACITY)
    with pytest.raises(IndexError):
        tree.range_set(0, 5, 0)  # left out of range
    with pytest.raises(IndexError):
        tree.range_set(5, HORIZON_DAYS + 1, 0)  # right out of range
    with pytest.raises(IndexError):
        tree.range_set(5, 3, 0)  # left > right


def test_segment_tree_point_update_raises_outside_range() -> None:
    import pytest
    from app.services.scheduling import SegmentTree

    tree = SegmentTree(n=HORIZON_DAYS, initial=DAILY_CAPACITY)
    with pytest.raises(IndexError):
        tree.point_update(0, 100)
    with pytest.raises(IndexError):
        tree.point_update(HORIZON_DAYS + 1, 100)


def test_segment_tree_from_array_rejects_wrong_length() -> None:
    """``from_array`` is the deserialization entry — a wrong-length input
    means the Redis blob is corrupted or written by a build with a
    different ``HORIZON_DAYS``. Better to raise loud than silently
    truncate / pad."""
    import pytest
    from app.services.scheduling import SegmentTree

    with pytest.raises(ValueError, match="expected"):
        SegmentTree.from_array([0] * (HORIZON_DAYS - 1))
    with pytest.raises(ValueError, match="expected"):
        SegmentTree.from_array([0] * (HORIZON_DAYS + 1))


# ---------------------------------------------------------------------------
# score_for_op unknown group
# ---------------------------------------------------------------------------


def test_score_for_op_rejects_unknown_group() -> None:
    """Defensive: prevents a typo / fabricated payload from picking a
    silently-wrong score region (would mis-order shrink vs grow without
    raising)."""
    import pytest
    from app.services.scheduling import score_for_op

    with pytest.raises(ValueError, match="unknown pending-op group"):
        score_for_op(group="cosmic", seq=1)


def test_score_for_op_shrink_sorts_before_grow() -> None:
    """The happy path: shrink-group scores must compare below grow-group
    scores for any same-seq pair, encoding the «shrink first» invariant
    that ``ZPOPMIN`` then enforces."""
    from app.services.scheduling import score_for_op

    assert score_for_op(group="shrink", seq=99) < score_for_op(group="grow", seq=1)
    # Within a group, score is monotonic in seq.
    assert score_for_op(group="shrink", seq=1) < score_for_op(group="shrink", seq=2)


# ---------------------------------------------------------------------------
# remove_order / _pq_remove_by_id defensive paths
# ---------------------------------------------------------------------------


def test_remove_order_rejects_deadline_outside_horizon() -> None:
    """If the order being removed has a deadline that's drifted out of
    the 30-day window (e.g. several advance_day cycles passed and the
    order was never cleaned out of pq), ``remove_order`` must return
    ``deadline_too_far`` instead of crashing on the tree math."""
    state = SchedulerState.initial(_BASE)
    ghost = SchedulingOrder(
        order_id=uuid.uuid4(),
        order_number="GHOST-REMOVE",
        wafer_quantity=100,
        deadline=_BASE - timedelta(days=1),
    )

    result = remove_order(state, ghost)
    assert result.status == "deadline_too_far"


# ---------------------------------------------------------------------------
# capacity_prefix_sums
# ---------------------------------------------------------------------------


def test_capacity_prefix_sums_returns_30_day_series() -> None:
    """``capacity_prefix_sums`` is the data source for ``GET
    /schedule/capacity`` — verify it returns exactly HORIZON_DAYS entries
    with monotonically increasing prefix sums and the right base-date
    alignment."""
    from app.services.scheduling import capacity_prefix_sums

    state = SchedulerState.initial(_BASE)
    series = capacity_prefix_sums(state)

    assert len(series) == HORIZON_DAYS
    # Empty state ⇒ prefix sum at day k is k * DAILY_CAPACITY.
    for i, (d, prefix) in enumerate(series, start=1):
        assert d == _BASE + timedelta(days=i - 1)
        assert prefix == i * DAILY_CAPACITY


# ---------------------------------------------------------------------------
# advance_day with future-day pin (covers the not-today pinned branch)
# ---------------------------------------------------------------------------


def test_advance_day_keeps_future_pinned_orders_with_shifted_rel() -> None:
    """A pin whose ``fake_deadline`` is NOT today must survive
    advance_day unchanged in identity, but reference a date that's now
    one day closer to the new base_date.

    Covers the ``pinned_remaining`` else-branch in ``advance_day`` —
    everything-today pin tests don't reach it.
    """
    state = SchedulerState.initial(_BASE)
    # Need an order in pq first so pin_order's precondition holds.
    pq_holder = _make_order(qty=500, deadline=_BASE + timedelta(days=10))
    add_order(state, pq_holder)
    pin_day = _BASE + timedelta(days=3)
    pin_result = pin_order(state, pq_holder, fake_deadline=pin_day)
    assert pin_result.status == "success"

    new_state = advance_day(state)

    # Pin survived; base advanced by 1; fake_deadline stays absolute.
    assert pq_holder.order_id in new_state.pinned_orders
    surviving = new_state.pinned_orders[pq_holder.order_id]
    assert surviving.fake_deadline == pin_day
    assert new_state.base_date == _BASE + timedelta(days=1)


def test_advance_day_handles_empty_pq_and_no_pins() -> None:
    """Boundary case: completely idle state. advance_day must still
    shift base_date by one and shift trees left without raising.
    Covers the ``boundary_order is None`` branch (line 1134) + the
    ``pinned_today_total == 0`` ceiling computation."""
    state = SchedulerState.initial(_BASE)

    new_state = advance_day(state)

    assert new_state.base_date == _BASE + timedelta(days=1)
    assert len(new_state.priority_queue) == 0
    assert new_state.pinned_orders == {}
    # Trees shifted: day 30 of the new state should be a fresh
    # DAILY_CAPACITY (the rolled-in tail day).
    assert new_state.capacity_tree.query(HORIZON_DAYS) == HORIZON_DAYS * DAILY_CAPACITY
