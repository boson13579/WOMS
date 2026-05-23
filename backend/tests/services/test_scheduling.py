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
    BatchOp,
    PinnedOrder,
    SchedulerState,
    SchedulingOrder,
    _apply_add_to_trees,
    _apply_remove_to_trees,
    _iter_pq_edf_sorted,
    _pq_add,
    abs_to_rel,
    add_order,
    advance_day,
    apply_batch_to_capacity,
    apply_batch_to_deadline,
    compute_batch_capacity_delta,
    compute_schedule,
    is_batch_feasible,
    pin_order,
    rebuild_state,
    rel_to_abs,
    remove_order,
    unpin_order,
)


def _seed_pq(state: SchedulerState, order: SchedulingOrder) -> None:
    """Pre-rule construction helper: drop ``order`` into ``state.pq`` and
    the trees without going through ``add_order``'s admission.

    Used by tests whose scenarios require day-1 occupancy (e.g.,
    ``advance_day`` processing today's orders). Production code can't put
    orders there anymore under ``FIRST_FILLABLE_DAY=2``, but the
    algorithm still has to handle pre-existing day-1 state correctly
    (legacy rows from before the rule shipped, or rebuild-state output
    on Redis snapshots that pre-date the rule).
    """
    _pq_add(state, order)
    _apply_add_to_trees(state, order)

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
    """Tree day 1 = ``base_date + 1`` (tomorrow), day 30 = ``base_date + 30``.
    ``base_date`` itself (= today) is out of tree, returns None.
    """
    # Range covers tomorrow (delta=1) through day 30 (delta=30).
    for delta in range(1, HORIZON_DAYS + 1):
        d = _BASE + timedelta(days=delta)
        rel = abs_to_rel(d, _BASE)
        assert rel == delta
        assert rel_to_abs(rel, _BASE) == d


def test_abs_to_rel_outside_horizon_returns_none() -> None:
    # Past dates.
    assert abs_to_rel(_BASE - timedelta(days=1), _BASE) is None
    # Today (= base_date) is now outside the tree under the new rule.
    assert abs_to_rel(_BASE, _BASE) is None
    # Past horizon end.
    assert abs_to_rel(_BASE + timedelta(days=HORIZON_DAYS + 1), _BASE) is None
    # Last day inside the horizon = base + 30, rel = 30.
    assert abs_to_rel(_BASE + timedelta(days=HORIZON_DAYS), _BASE) == HORIZON_DAYS
    # First day inside the horizon = base + 1, rel = 1.
    assert abs_to_rel(_BASE + timedelta(days=1), _BASE) == 1


# ---------------------------------------------------------------------------
# add_order
# ---------------------------------------------------------------------------


def test_add_order_success_updates_both_trees() -> None:
    """Tree day 1 = base_date + 1, so deadline = base_date + 2 → rel = 2."""
    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=2000, deadline=_BASE + timedelta(days=2))  # rel = 2

    result = add_order(state, order)

    assert result.status == "success"
    assert result.order_id == order.order_id
    assert order.order_id in state.pq_index
    # capacity_tree prefix at rel=2 reflects the 2000 backward-filled.
    assert state.capacity_tree.query(2) == 2 * DAILY_CAPACITY - 2000
    assert state.capacity_tree.query(HORIZON_DAYS) == HORIZON_DAYS * DAILY_CAPACITY - 2000
    # deadline_tree carries the order's quantity at its deadline index (rel=2).
    assert state.deadline_tree.query(2) == 2000


def test_add_order_capacity_exceeded() -> None:
    """Deadline = today is now ``deadline_too_far`` (out-of-tree) instead of
    ``capacity_exceeded``. The pure "qty too large" case is exercised in
    :func:`test_add_order_capacity_exceeded_when_qty_exceeds_window` below.
    """
    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=20_000, deadline=_BASE)

    result = add_order(state, order)

    assert result.status == "deadline_too_far"
    assert "today" in result.message.lower()
    assert order.order_id not in state.pq_index
    # Trees untouched.
    assert state.capacity_tree.query(1) == DAILY_CAPACITY
    assert state.deadline_tree.query(1) == 0


def test_add_order_capacity_exceeded_when_qty_exceeds_window() -> None:
    """Quantity exceeding the capacity between tomorrow and deadline:
    rejects with ``capacity_exceeded``. Tree day 1 = tomorrow with
    capacity = DAILY_CAPACITY (10000); a 20,000-wafer order with
    deadline = tomorrow has only 10,000 fillable → reject.
    """
    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=20_000, deadline=_BASE + timedelta(days=1))  # rel = 1

    result = add_order(state, order)

    assert result.status == "capacity_exceeded"
    assert order.order_id not in state.pq_index
    assert state.capacity_tree.query(1) == DAILY_CAPACITY
    assert state.deadline_tree.query(1) == 0


def test_add_order_deadline_too_far() -> None:
    """Deadline past the horizon end (base + 30 = last valid, base + 31 = too far)."""
    state = SchedulerState.initial(_BASE)
    order = _make_order(
        qty=1000,
        deadline=_BASE + timedelta(days=HORIZON_DAYS + 1),  # one day past the horizon end
    )

    result = add_order(state, order)

    assert result.status == "deadline_too_far"
    assert order.order_id not in state.pq_index
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

    assert state.pq_index == {}
    # Every prefix sum back to the original full-capacity state
    for d in range(1, HORIZON_DAYS + 1):
        assert state.capacity_tree.query(d) == d * DAILY_CAPACITY
        assert state.deadline_tree.query(d) == 0


def test_remove_order_leaves_other_orders_intact() -> None:
    """Doc example abc on day 2 — remove the middle one and verify what's left.

    Originally written against day-1 fills; under the ``FIRST_FILLABLE_DAY = 2``
    rule (today is locked from new admissions) day-1 is unreachable, so we
    shift the deadline to ``base + 1`` (tomorrow = rel 2) and assert against
    day-2 prefix sums. The remove-then-check invariant is unchanged.
    """
    state = SchedulerState.initial(_BASE)
    dl = _BASE + timedelta(days=1)
    a = _make_order(order_number="a", qty=2000, deadline=dl)
    b = _make_order(order_number="b", qty=2000, deadline=dl)
    c = _make_order(order_number="c", qty=2000, deadline=dl)
    for o in (a, b, c):
        assert add_order(state, o).status == "success"

    # Sanity: capacity prefix at day 2 = 20000 - 6000 = 14000
    assert state.capacity_tree.query(2) == 14000
    assert state.deadline_tree.query(2) == 6000

    remove_order(state, b)

    # Only a + c remain → 4000 used, 16000 free at prefix day 2
    assert state.capacity_tree.query(2) == 16000
    assert state.deadline_tree.query(2) == 4000
    pq_ids = set(state.pq_index.keys())
    assert pq_ids == {a.order_id, c.order_id}


def test_remove_order_restores_when_later_add_overlaps_earlier_one() -> None:
    """Regression test for a multi-add overlap case under the new
    tree convention (tree day 1 = base_date + 1 / tomorrow).

    Scenario:
      1. Add order_first (qty=10_000, deadline=base+2, rel=2). Backward-fill
         lands all 10_000 on tree day 2 → raw [10_000, 0, 10_000, ...].
      2. Add order_second (qty=15_000, deadline=base+3, rel=3). Backward-fill
         zeroes tree day 3 (10k) then cascades to tree day 1 (-5k) →
         raw [5_000, 0, 0, 10_000, ...].
      3. Remove order_second. Trees must roll back to the post-step-1 state
         exactly, NOT to a naive "split 15_000 evenly" result.

    Guards the per-day slack recomputation invariant in the give-back loop.
    """
    state = SchedulerState.initial(_BASE)
    first = _make_order(order_number="first", qty=10_000, deadline=_BASE + timedelta(days=2))
    second = _make_order(order_number="second", qty=15_000, deadline=_BASE + timedelta(days=3))

    assert add_order(state, first).status == "success"
    # Sanity: capacity prefix after first add.
    assert state.capacity_tree.query(1) == 10_000  # day 1 untouched
    assert state.capacity_tree.query(2) == 10_000  # day 2 fully consumed by first
    assert state.capacity_tree.query(3) == 20_000
    assert state.capacity_tree.query(4) == 30_000

    assert add_order(state, second).status == "success"
    # Sanity: capacity prefix after second add (cascade reached day 1).
    assert state.capacity_tree.query(1) == 5_000
    assert state.capacity_tree.query(2) == 5_000
    assert state.capacity_tree.query(3) == 5_000
    assert state.capacity_tree.query(4) == 15_000

    remove_order(state, second)

    # After remove, state must equal "after first only".
    assert state.capacity_tree.query(1) == 10_000
    assert state.capacity_tree.query(2) == 10_000
    assert state.capacity_tree.query(3) == 20_000
    assert state.capacity_tree.query(4) == 30_000
    for d in range(5, HORIZON_DAYS + 1):
        assert state.capacity_tree.query(d) == d * DAILY_CAPACITY - 10_000

    # deadline_tree: only first's 10_000 obligation at rel=2 remains.
    assert state.deadline_tree.query(1) == 0
    for d in range(2, HORIZON_DAYS + 1):
        assert state.deadline_tree.query(d) == 10_000

    # pq_index: only first remains
    assert len(state.pq_index) == 1
    assert _iter_pq_edf_sorted(state)[0].order_id == first.order_id


# ---------------------------------------------------------------------------
# compute_schedule — split across days
# ---------------------------------------------------------------------------


def test_compute_schedule_splits_orders_across_days() -> None:
    """Doc example abc, shifted forward one day under the
    ``FIRST_FILLABLE_DAY = 2`` rule (today is locked; forward-fill starts
    at tomorrow). a's 15,000 wafers cross the day-2 / day-3 boundary
    instead of the original day-1 / day-2 boundary; b spills into day 4;
    c gets the day-4 tail. Same EDF + tie-break logic, just one calendar
    day later.
    """
    state = SchedulerState.initial(_BASE)
    a = _make_order(order_number="a", qty=15_000, deadline=_BASE + timedelta(days=2))
    b = _make_order(order_number="b", qty=8_000, deadline=_BASE + timedelta(days=3))
    c = _make_order(order_number="c", qty=2_000, deadline=_BASE + timedelta(days=3))

    for o in (a, b, c):
        assert add_order(state, o).status == "success"

    results = compute_schedule(state)

    by_order: dict[uuid.UUID, dict[date, int]] = {}
    for r in results:
        by_order.setdefault(r.order_id, {})[r.scheduled_date] = r.quantity

    # a: 10,000 on day 2 (= base+1), 5,000 on day 3 (= base+2)
    assert by_order[a.order_id] == {
        _BASE + timedelta(days=1): 10_000,
        _BASE + timedelta(days=2): 5_000,
    }
    # b: 5,000 on day 3 (after a), 3,000 on day 4
    assert by_order[b.order_id] == {
        _BASE + timedelta(days=2): 5_000,
        _BASE + timedelta(days=3): 3_000,
    }
    # c: 2,000 on day 4
    assert by_order[c.order_id] == {_BASE + timedelta(days=3): 2_000}


# ---------------------------------------------------------------------------
# advance_day — full doc example (abc / de / fg with daily-cap boundary on f)
# ---------------------------------------------------------------------------


def test_advance_day_processes_pq_and_shifts_trees() -> None:
    """Doc example abc / de / fg with abc on tree day 1.

    Under the new convention (tree day 1 = ``base_date + 1``), to put
    orders on tree day 1 they must have deadline = ``base + 1``. We
    initialize state with ``base = _BASE`` and use abc deadlines =
    ``_BASE + 1`` (tomorrow from base's POV = tree day 1). advance_day
    then processes them as "today's orders" — same algorithmic behavior
    as the original doc example, just with deadlines that the new
    admission rule will accept.
    """
    state = SchedulerState.initial(_BASE)
    a = _make_order(order_number="a", qty=2000, deadline=_BASE + timedelta(days=1))
    b = _make_order(order_number="b", qty=2000, deadline=_BASE + timedelta(days=1))
    c = _make_order(order_number="c", qty=2000, deadline=_BASE + timedelta(days=1))
    d = _make_order(order_number="d", qty=1000, deadline=_BASE + timedelta(days=2))
    e = _make_order(order_number="e", qty=2000, deadline=_BASE + timedelta(days=2))
    f = _make_order(order_number="f", qty=2000, deadline=_BASE + timedelta(days=3))
    g = _make_order(order_number="g", qty=2000, deadline=_BASE + timedelta(days=3))

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
    assert len(new_state.pq_index) == 2
    surviving = dict(new_state.pq_index)
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
    edf = _iter_pq_edf_sorted(new_state)
    assert edf[0].order_id == g.order_id
    assert edf[1].order_id == f.order_id

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
    assert len(state.pq_index) == 7


# ---------------------------------------------------------------------------
# rebuild_state — recovery from clean slate
# ---------------------------------------------------------------------------


def test_rebuild_state_empty_orders_returns_empty_state() -> None:
    state, skipped = rebuild_state([], _BASE)

    assert state.base_date == _BASE
    assert state.pq_index == {}
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
    assert len(rebuilt.pq_index) == 1
    assert _iter_pq_edf_sorted(rebuilt)[0].order_id == order.order_id
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
    edf = _iter_pq_edf_sorted(rebuilt)
    assert edf[0].order_id == b.order_id
    assert edf[1].order_id == a.order_id
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
    # Tree day 30 = base + 30 is the last valid; base + 31 is too far.
    outside = _make_order(
        order_number="outside", qty=500, deadline=_BASE + timedelta(days=HORIZON_DAYS + 1)
    )

    state, skipped = rebuild_state([inside, outside], _BASE)

    pq_ids = set(state.pq_index.keys())
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
    pq_len_before = len(state.pq_index)

    second = add_order(state, order)
    assert second.status == "capacity_exceeded"
    # Critical: state is UNCHANGED by the rejected duplicate.
    assert state.capacity_tree.to_array() == cap_before
    assert state.deadline_tree.to_array() == dead_before
    assert len(state.pq_index) == pq_len_before


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
    assert order.order_id not in state.pq_index

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
    pq_before = dict(state.pq_index)

    phantom = _make_order(order_number="phantom", qty=500, deadline=_BASE + timedelta(days=2))
    result = remove_order(state, phantom)
    assert result.status == "capacity_exceeded"
    # State unchanged.
    assert state.capacity_tree.to_array() == cap_before
    assert state.deadline_tree.to_array() == dead_before
    assert state.pq_index == pq_before


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
    """Spec example 1, shifted +1 day under the ``FIRST_FILLABLE_DAY = 2``
    rule: existing (a 9000 dl=2) + (b 2000 dl=3). Pin b to day 2 must fail
    because day 2 only has ``10000-9000=1000`` free, b needs 2000.

    Critically: state must be UNCHANGED after rejection. The pin path
    speculatively removes the order from pq+trees and re-adds at the fake
    day; on capacity failure it has to undo cleanly. Without that undo, a
    rejected pin would silently drop the order.
    """
    state = SchedulerState.initial(_BASE)
    a = _make_order(order_number="a", qty=9000, deadline=_BASE + timedelta(days=1))
    b = _make_order(order_number="b", qty=2000, deadline=_BASE + timedelta(days=2))
    add_order(state, a)
    add_order(state, b)

    # Snapshot trees + pq for post-rejection comparison.
    cap_before = state.capacity_tree.to_array()
    dead_before = state.deadline_tree.to_array()
    pq_ids_before = set(state.pq_index.keys())

    result = pin_order(state, b, fake_deadline=_BASE + timedelta(days=1))
    assert result.status == "capacity_exceeded"

    assert state.capacity_tree.to_array() == cap_before
    assert state.deadline_tree.to_array() == dead_before
    assert set(state.pq_index.keys()) == pq_ids_before
    assert state.pinned_orders == {}


def test_pin_order_success_matches_spec_example_2() -> None:
    """Spec example 2, shifted +1 day under the ``FIRST_FILLABLE_DAY = 2``
    rule: (a 9000 dl=4), (b 1000 dl=4), (c 1000 dl=4), pin b to day 2 then
    c to day 2. Day 1 is locked from new admissions and from pin (pin to
    today = a "modification" of today's commitment, which is exactly what
    the rule forbids), so the spec example's "pin to day 1" becomes "pin
    to day 2 = tomorrow" — the earliest pinnable day. The relative
    structure (a 9000 at horizon, b+c 1000 each pinned to the same earlier
    day) is unchanged; only the calendar offset shifts.

    After both pins:
      * day 1 = locked, no contribution (capacity stays 10000)
      * day 2 = pinned b+c (2000 consumed)
      * a's 9000 backward-fills toward day 4

    pq holds {a}; pinned_orders holds {b, c}.
    """
    state = SchedulerState.initial(_BASE)
    deadline_4 = _BASE + timedelta(days=3)  # rel = 4
    pin_day = _BASE + timedelta(days=1)  # rel = 2 = first pinnable day
    a = _make_order(order_number="a", qty=9000, deadline=deadline_4)
    b = _make_order(order_number="b", qty=1000, deadline=deadline_4)
    c = _make_order(order_number="c", qty=1000, deadline=deadline_4)
    add_order(state, a)
    add_order(state, b)
    add_order(state, c)

    pin_b = pin_order(state, b, fake_deadline=pin_day)
    assert pin_b.status == "success"
    pin_c = pin_order(state, c, fake_deadline=pin_day)
    assert pin_c.status == "success"

    # Membership invariant: a stays in pq, b+c moved to pinned_orders.
    pq_ids = set(state.pq_index.keys())
    pinned_ids = set(state.pinned_orders.keys())
    assert pq_ids == {a.order_id}
    assert pinned_ids == {b.order_id, c.order_id}
    # Tree day 1 (= base+1) carries b+c's pinned contribution (2000).
    # deadline_tree's prefix shows the pinned contribution at rel=1 and
    # a's contribution at rel=3.
    assert state.capacity_tree.query(1) == DAILY_CAPACITY - 2000
    assert state.deadline_tree.query(1) == 2000
    # All three orders' deadline contributions accounted at horizon endpoint.
    assert state.deadline_tree.query(3) == 2000 + 9000


def test_unpin_order_restores_state_to_pre_pin() -> None:
    """Pin b+c to tree day 1, then unpin c. After unpin: c is back in pq
    with the original deadline; pinned_orders holds only b. Verifies that
    unpin_order correctly reverses the tree manipulation that pin_order
    did.
    """
    state = SchedulerState.initial(_BASE)
    deadline_3 = _BASE + timedelta(days=3)
    pin_day = _BASE + timedelta(days=1)  # rel=1 (tree day 1 = tomorrow)
    a = _make_order(order_number="a", qty=9000, deadline=deadline_3)
    b = _make_order(order_number="b", qty=1000, deadline=deadline_3)
    c = _make_order(order_number="c", qty=1000, deadline=deadline_3)
    for o in (a, b, c):
        add_order(state, o)
    pin_order(state, b, fake_deadline=pin_day)
    pin_order(state, c, fake_deadline=pin_day)

    result = unpin_order(state, c.order_id)
    assert result.status == "success"

    # After unpin: only b stays pinned (to rel=1), a + c back in pq.
    pq_ids = set(state.pq_index.keys())
    pinned_ids = set(state.pinned_orders.keys())
    assert pq_ids == {a.order_id, c.order_id}
    assert pinned_ids == {b.order_id}
    # Tree day 1 still has b's pinned 1000 in deadline_tree.
    assert state.deadline_tree.query(1) == 1000


def test_unpin_order_unknown_id_returns_error_without_mutating_state() -> None:
    """Calling unpin on an id not in ``pinned_orders`` is a logic error from
    the producer side; treat as a soft failure so the worker's failure-notify
    path runs, and leave the pq + trees alone.
    """
    state = SchedulerState.initial(_BASE)
    add_order(state, _make_order(order_number="a", qty=1000, deadline=_BASE + timedelta(days=2)))

    cap_before = state.capacity_tree.to_array()
    pq_before = dict(state.pq_index)
    pinned_before = dict(state.pinned_orders)

    result = unpin_order(state, uuid.uuid4())
    assert result.status == "capacity_exceeded"
    assert state.capacity_tree.to_array() == cap_before
    assert state.pq_index == pq_before
    assert state.pinned_orders == pinned_before


def test_compute_schedule_places_pinned_first_then_fills_pq() -> None:
    """Two-phase fill: pinned consumes its fake_deadline day first, then EDF
    fills post-pin remaining. Pin target shifted to day 2 (= base+1) under
    ``FIRST_FILLABLE_DAY = 2``; b+c pinned to day 2, a (9000) spreads from
    day 2 onward.

    Expected:
      * Day 2: b1000 + c1000 + a8000 = 10000
      * Day 3: a's remaining 1000
    """
    state = SchedulerState.initial(_BASE)
    deadline_4 = _BASE + timedelta(days=3)
    pin_day = _BASE + timedelta(days=1)  # rel=2
    a = _make_order(order_number="a", qty=9000, deadline=deadline_4)
    b = _make_order(order_number="b", qty=1000, deadline=deadline_4)
    c = _make_order(order_number="c", qty=1000, deadline=deadline_4)
    for o in (a, b, c):
        add_order(state, o)
    pin_order(state, b, fake_deadline=pin_day)
    pin_order(state, c, fake_deadline=pin_day)

    schedule = compute_schedule(state)

    by_day = {(r.scheduled_date, r.order_id): r.quantity for r in schedule}
    # Day 2: b1000 + c1000 + a8000 (pinned phase 1, then a's forward fill).
    assert by_day[(pin_day, b.order_id)] == 1000
    assert by_day[(pin_day, c.order_id)] == 1000
    assert by_day[(pin_day, a.order_id)] == 8000
    # Day 3: a's remaining 1000.
    assert by_day[(_BASE + timedelta(days=2), a.order_id)] == 1000
    # Day 1 (today) must be empty under the new rule.
    assert all(r.scheduled_date != _BASE for r in schedule)


def test_advance_day_completes_pinned_today_and_fills_remainder_from_pq() -> None:
    """advance_day's "pinned-today" branch: pinned x at tree day 1 (=
    base_date + 1) is produced in full at the midnight transition. The
    pq accumulator's ceiling for the same day = ``DAILY_CAPACITY -
    pinned_today_total``, so a co-existing pq order y absorbs the
    remainder. The test uses ``base_date = _BASE - 1`` so that
    ``_BASE`` is tree day 1 — the legal pin target under the new rule
    and the day that advance_day's Step 0 processes.
    """
    yesterday = _BASE - timedelta(days=1)
    state = SchedulerState.initial(yesterday)
    # Both orders fit in the new tree (rel=2 from yesterday's base).
    x = _make_order(order_number="x", qty=2000, deadline=_BASE + timedelta(days=1))
    y = _make_order(order_number="y", qty=15000, deadline=_BASE + timedelta(days=1))
    add_order(state, x)
    add_order(state, y)
    # Pin x to ``_BASE`` (= tree day 1 of state with base=yesterday).
    assert pin_order(state, x, fake_deadline=_BASE).status == "success"

    new_state = advance_day(state)

    # Pinned x is gone — produced today.
    assert new_state.pinned_orders == {}
    # y remains in pq with qty reduced by 8000 (the pq budget after pinned-today).
    assert len(new_state.pq_index) == 1
    edf = _iter_pq_edf_sorted(new_state)
    assert edf[0].order_id == y.order_id
    assert edf[0].wafer_quantity == 15000 - 8000
    # base_date advanced by 1 day (yesterday → today).
    assert new_state.base_date == _BASE


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
    pin_day = _BASE + timedelta(days=1)  # rel=2 = earliest pinnable under new rule
    b = SchedulingOrder(
        order_id=uuid.uuid4(),
        order_number="b",
        wafer_quantity=1000,
        deadline=deadline_3,
        pinned_production_date=pin_day,  # marks this for the pinned path
    )
    state, skipped = rebuild_state([a, b], _BASE)

    assert skipped == []
    pq_ids = set(state.pq_index.keys())
    pinned_ids = set(state.pinned_orders.keys())
    assert pq_ids == {a.order_id}
    assert pinned_ids == {b.order_id}
    # Pinned order is recorded with both real + fake deadlines for unpin.
    pinned_b = state.pinned_orders[b.order_id]
    assert pinned_b.deadline == deadline_3
    assert pinned_b.fake_deadline == pin_day


# ---------------------------------------------------------------------------
# Admission control invariants — "day 1 (today) locked from new admissions"
# ---------------------------------------------------------------------------
#
# Under ``FIRST_FILLABLE_DAY = 2``, every new ``add_order`` / ``pin_order``
# that would touch day 1 rejects at admission. The earlier P1-1 invariant
# tests (pin full today + add today, etc.) tested "what if both end up
# coexisting" — that scenario is no longer reachable via the public API,
# so we replace those tests with checks of the admission rejection itself.
#
# Mental model: ``capacity_tree.query(rel) - capacity_tree.query(1)`` is
# the new feasibility metric — capacity available in [day 2 .. day rel].
# ``rel == 1`` makes this 0 trivially, so any positive ``wafer_quantity``
# rejects. The same subtraction applies in ``pin_order``.


def test_add_order_rejects_deadline_today() -> None:
    """An order with deadline = today (``rel = 1``) has no usable days under
    the new rule — the segment tree starts at ``base_date + 1`` (tomorrow),
    so ``abs_to_rel(today, base_date)`` returns ``None`` → admission
    rejects as ``deadline_too_far``. State must remain untouched.
    """
    state = SchedulerState.initial(_BASE)
    cap_before = state.capacity_tree.to_array()
    dead_before = state.deadline_tree.to_array()

    # Even a 1-wafer order due today is rejected — the bound is structural,
    # not quantity-based.
    order = _make_order(qty=1, deadline=_BASE)
    result = add_order(state, order)

    assert result.status == "deadline_too_far"
    # Rejection message distinguishes the "due today" subcase from the
    # generic "outside horizon" so the UI can show an actionable hint.
    assert "today" in result.message.lower()
    assert order.order_id not in state.pq_index
    assert state.capacity_tree.to_array() == cap_before
    assert state.deadline_tree.to_array() == dead_before


def test_add_order_rejects_when_only_day_one_has_capacity() -> None:
    """Even with the entire 30-day horizon free, an order whose deadline is
    today has no fillable day — the tree starts at tomorrow. This is the
    invariant that protects ``compute_schedule``'s forward-fill from
    emitting today-rows.
    """
    state = SchedulerState.initial(_BASE)
    # Sanity: full horizon capacity is available...
    assert state.capacity_tree.query(HORIZON_DAYS) == HORIZON_DAYS * DAILY_CAPACITY
    # ...but ``deadline = today`` has no tree index at all (rel = None).
    order = _make_order(qty=100, deadline=_BASE)
    assert add_order(state, order).status == "deadline_too_far"


def test_pin_order_rejects_pin_to_today() -> None:
    """Pinning to today is rejected through the same ``abs_to_rel`` gate
    that rejects deadline = today in ``add_order``: tree day 1 = tomorrow,
    so ``fake_deadline = today`` falls outside the tree entirely. The
    rejection branch must NOT have spuriously mutated the speculative-
    remove state — ``pin_order``'s early bail (before the speculative
    remove) is what protects against that.
    """
    state = SchedulerState.initial(_BASE)
    # Order eligible for pin (already in pq, future deadline).
    order = _make_order(
        order_number="Y",
        qty=1_000,
        deadline=_BASE + timedelta(days=10),
    )
    assert add_order(state, order).status == "success"
    cap_before = state.capacity_tree.to_array()
    dead_before = state.deadline_tree.to_array()
    pq_before = dict(state.pq_index)

    result = pin_order(state, order, fake_deadline=_BASE)

    assert result.status == "deadline_too_far"
    assert "today" in result.message.lower()
    assert state.pinned_orders == {}
    # No speculative mutation happened — pin's early ``rel is None`` bail
    # short-circuits before the remove/add dance.
    assert state.capacity_tree.to_array() == cap_before
    assert state.deadline_tree.to_array() == dead_before
    assert state.pq_index == pq_before


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
    """Pin with ``fake_deadline`` beyond the 30-day window (= ``base + 30``
    is the last valid pinnable day, ``base + 31`` is too far) must return
    ``deadline_too_far`` without mutating state.
    """
    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=500, deadline=_BASE + timedelta(days=HORIZON_DAYS))
    assert add_order(state, order).status == "success"
    snapshot = state.to_json()

    far_pin_day = _BASE + timedelta(days=HORIZON_DAYS + 1)  # one past horizon end
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
    assert pinned.order_id not in set(state.pq_index.keys())


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
    # Pin both orders to ``base_date + 1`` (rel=2, the earliest pinnable
    # day under ``FIRST_FILLABLE_DAY = 2``). For the new admission
    # arithmetic ``query(fake_rel) - query(1)``, fake_rel=2 means only
    # day-2's own capacity counts. The first pin exhausts it; the second
    # pin then has 0 available and rejects.
    pin_day = _BASE + timedelta(days=1)
    first = SchedulingOrder(
        order_id=uuid.uuid4(),
        order_number="REBUILD-PIN-A",
        wafer_quantity=DAILY_CAPACITY,  # fills day-2 entirely after pin
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
    pq_ids = set(new_state.pq_index.keys())
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
    alignment. Tree day 1 = ``base_date + 1`` (tomorrow), so the series
    starts at tomorrow.
    """
    from app.services.scheduling import capacity_prefix_sums

    state = SchedulerState.initial(_BASE)
    series = capacity_prefix_sums(state)

    assert len(series) == HORIZON_DAYS
    # Empty state ⇒ prefix sum at day k is k * DAILY_CAPACITY.
    # Date series starts at ``base_date + 1`` (tree day 1 = tomorrow).
    for i, (d, prefix) in enumerate(series, start=1):
        assert d == _BASE + timedelta(days=i)
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
    assert len(new_state.pq_index) == 0
    assert new_state.pinned_orders == {}
    # Trees shifted: day 30 of the new state should be a fresh
    # DAILY_CAPACITY (the rolled-in tail day).
    assert new_state.capacity_tree.query(HORIZON_DAYS) == HORIZON_DAYS * DAILY_CAPACITY


# ---------------------------------------------------------------------------
# Batch admission — compute_batch_capacity_delta
# ---------------------------------------------------------------------------


def test_compute_batch_delta_add_only_lands_on_deadline_day() -> None:
    """A single add op contributes +qty at its deadline's rel-1 index
    (0-based). Tree day 1 = base_date + 1, so deadline = base_date + 3
    gives rel = 3 → delta[2].
    """
    ops = [BatchOp(kind="add", wafer_quantity=500, deadline=_BASE + timedelta(days=3))]

    delta = compute_batch_capacity_delta(ops, _BASE)

    assert len(delta) == HORIZON_DAYS
    # deadline = base + 3 → rel = 3 → index 2 (0-based).
    assert delta[2] == 500
    assert all(d == 0 for i, d in enumerate(delta) if i != 2)


def test_compute_batch_delta_remove_negates_quantity() -> None:
    """Remove contributes ``-wafer_quantity`` at the deadline day (rel-1)."""
    ops = [BatchOp(kind="remove", wafer_quantity=300, deadline=_BASE + timedelta(days=5))]

    delta = compute_batch_capacity_delta(ops, _BASE)

    # deadline = base + 5 → rel = 5 → index 4.
    assert delta[4] == -300
    assert sum(abs(d) for i, d in enumerate(delta) if i != 4) == 0


def test_compute_batch_delta_patch_self_cancels_when_same_day() -> None:
    """A PATCH (remove old + add new) on the same deadline day with the
    same quantity nets to zero — the batch is a no-op for capacity
    accounting even though pq / DB rows still change."""
    same_day = _BASE + timedelta(days=4)
    ops = [
        BatchOp(kind="remove", wafer_quantity=200, deadline=same_day),
        BatchOp(kind="add", wafer_quantity=200, deadline=same_day),
    ]

    delta = compute_batch_capacity_delta(ops, _BASE)

    assert all(d == 0 for d in delta)


def test_compute_batch_delta_deadline_shift_splits_across_days() -> None:
    """PATCH that shifts a deadline from day A to day B (same qty) lands
    -qty on day A and +qty on day B — the two cancel in total but the
    per-day distribution matters for feasibility."""
    ops = [
        BatchOp(kind="remove", wafer_quantity=400, deadline=_BASE + timedelta(days=2)),
        BatchOp(kind="add", wafer_quantity=400, deadline=_BASE + timedelta(days=10)),
    ]

    delta = compute_batch_capacity_delta(ops, _BASE)

    # rel-1 indices: 2-1=1, 10-1=9.
    assert delta[1] == -400
    assert delta[9] == 400


def test_compute_batch_delta_drops_ops_outside_horizon() -> None:
    """Op with deadline outside ``[base+1, base+30]`` is silently skipped.
    Today (= base_date) is now outside the tree under the new rule, so
    it also gets dropped — same path as the past / too-far cases.
    """
    ops = [
        BatchOp(kind="add", wafer_quantity=500, deadline=_BASE - timedelta(days=1)),  # past
        BatchOp(
            kind="add",
            wafer_quantity=500,
            deadline=_BASE + timedelta(days=HORIZON_DAYS + 1),  # too far
        ),
        BatchOp(kind="add", wafer_quantity=999, deadline=_BASE),  # today — also dropped
        # The one valid op: deadline = base + 2 → rel = 2 → index 1.
        BatchOp(kind="add", wafer_quantity=100, deadline=_BASE + timedelta(days=2)),
    ]

    delta = compute_batch_capacity_delta(ops, _BASE)

    assert delta[1] == 100  # only the in-horizon op contributed
    assert sum(d for i, d in enumerate(delta) if i != 1) == 0


def test_compute_batch_delta_multiple_ops_same_day_sum() -> None:
    """Three ops on the same day all add into the same delta cell."""
    day = _BASE + timedelta(days=7)
    ops = [
        BatchOp(kind="add", wafer_quantity=100, deadline=day),
        BatchOp(kind="add", wafer_quantity=250, deadline=day),
        BatchOp(kind="remove", wafer_quantity=50, deadline=day),
    ]

    delta = compute_batch_capacity_delta(ops, _BASE)

    # deadline = base + 7 → rel = 7 → index 6.
    assert delta[6] == 100 + 250 - 50


def test_compute_batch_delta_empty_input_returns_zeros() -> None:
    """No ops ⇒ all-zero delta. Sanity check + degenerate base case."""
    delta = compute_batch_capacity_delta([], _BASE)

    assert delta == [0] * HORIZON_DAYS


# ---------------------------------------------------------------------------
# Batch admission — is_batch_feasible
# ---------------------------------------------------------------------------


def test_is_batch_feasible_empty_delta_is_feasible() -> None:
    """All-zero delta is always feasible — no demand to compare against."""
    state = SchedulerState.initial(_BASE)
    assert is_batch_feasible(state, [0] * HORIZON_DAYS) is True


def test_is_batch_feasible_demand_under_capacity_passes() -> None:
    """Demand prefix strictly less than capacity prefix on every day ⇒
    feasible. Day 1 is locked (capacity contribution = 0 under
    ``FIRST_FILLABLE_DAY = 2``), so the day-1 demand entry must be 0;
    days 2+ each take well under ``DAILY_CAPACITY``.
    """
    state = SchedulerState.initial(_BASE)
    delta = [0] + [100] * (HORIZON_DAYS - 1)  # day 1 locked; 100/day from day 2

    assert is_batch_feasible(state, delta) is True


def test_is_batch_feasible_demand_equals_capacity_passes() -> None:
    """Demand exactly at the prefix-sum boundary is feasible (``<=``, not
    strict). Validates the inequality direction. Day 1 capacity is 0
    under the new rule, so the canonical "equal-to-capacity" demand
    targets day 2 instead.
    """
    state = SchedulerState.initial(_BASE)
    delta = [0, DAILY_CAPACITY] + [0] * (HORIZON_DAYS - 2)  # exactly fills day 2

    assert is_batch_feasible(state, delta) is True


def test_is_batch_feasible_single_day_over_capacity_fails() -> None:
    """One day's prefix sum exceeding the capacity prefix sum is enough
    to reject the whole batch."""
    state = SchedulerState.initial(_BASE)
    # Demand DAILY_CAPACITY + 1 on day 1 — one unit over.
    delta = [DAILY_CAPACITY + 1] + [0] * (HORIZON_DAYS - 1)

    assert is_batch_feasible(state, delta) is False


def test_is_batch_feasible_later_day_violation_caught() -> None:
    """Demand on a later day pushing cumulative prefix past cumulative
    capacity prefix should be detected (not just the first day)."""
    state = SchedulerState.initial(_BASE)
    # Days 1-4 take all the cumulative cap. Day 5 demand is +1 over.
    delta = [DAILY_CAPACITY] * 5 + [0] * (HORIZON_DAYS - 5)
    delta[4] += 1  # day 5 over

    assert is_batch_feasible(state, delta) is False


def test_is_batch_feasible_negative_prefix_passes_trivially() -> None:
    """If the batch nets to a removal (negative cumulative demand at some
    day), feasibility is automatic at that day. Validates that the
    inequality stays correct for sign-mixed batches."""
    state = SchedulerState.initial(_BASE)
    # Day 3: -500 (the batch removes existing demand)
    delta = [0, 0, -500] + [0] * (HORIZON_DAYS - 3)

    assert is_batch_feasible(state, delta) is True


def test_is_batch_feasible_wrong_length_raises() -> None:
    """API contract: delta must be exactly HORIZON_DAYS long."""
    state = SchedulerState.initial(_BASE)
    import pytest

    with pytest.raises(ValueError, match=str(HORIZON_DAYS)):
        is_batch_feasible(state, [0] * (HORIZON_DAYS - 1))


# ---------------------------------------------------------------------------
# Batch admission — apply_batch_to_capacity (carry-back)
# ---------------------------------------------------------------------------


def test_apply_batch_to_capacity_noop_for_zero_delta() -> None:
    """All-zero delta leaves the tree bit-identical."""
    state = SchedulerState.initial(_BASE)
    before = state.capacity_tree.to_array()

    apply_batch_to_capacity(state, [0] * HORIZON_DAYS)

    assert state.capacity_tree.to_array() == before


def test_apply_batch_to_capacity_single_add_distributes_via_carry_back() -> None:
    """Adding qty < DAILY_CAPACITY on a single day reduces that day's
    raw remaining by exactly qty (no spill-back needed). The prefix-sum
    semantics match: ``prefix(HORIZON_DAYS) decreased by qty``."""
    state = SchedulerState.initial(_BASE)
    qty = 1500
    delta = [0] * HORIZON_DAYS
    delta[4] = qty  # day 5

    apply_batch_to_capacity(state, delta)

    raw = state.capacity_tree.to_array()
    # Day 5 absorbed all qty (it had DAILY_CAPACITY available, qty < cap).
    assert raw[4] == DAILY_CAPACITY - qty
    # Earlier days untouched (no spill-back).
    assert all(r == DAILY_CAPACITY for r in raw[:4])
    # Later days untouched.
    assert all(r == DAILY_CAPACITY for r in raw[5:])


def test_apply_batch_to_capacity_overflow_spills_to_earlier_days() -> None:
    """If a day's demand exceeds its remaining capacity, the carry-back
    formula moves the overflow to earlier days. Shifted +1 day under
    ``FIRST_FILLABLE_DAY = 2``: demand on day 3 (1.5x cap) spills into
    day 2; ``is_batch_feasible`` accepts because cumulative capacity at
    day 3 = 0 (day 1 locked) + 10000 (day 2) + 10000 (day 3) = 20000 >=
    15000. Day 1 stays locked at full capacity.
    """
    state = SchedulerState.initial(_BASE)
    overflow_qty = DAILY_CAPACITY + (DAILY_CAPACITY // 2)
    delta = [0, 0, overflow_qty] + [0] * (HORIZON_DAYS - 3)

    assert is_batch_feasible(state, delta) is True

    apply_batch_to_capacity(state, delta)

    raw = state.capacity_tree.to_array()
    # Day 3 absorbed DAILY_CAPACITY (its full slot), day 2 absorbed the rest.
    assert raw[2] == 0
    assert raw[1] == DAILY_CAPACITY - (DAILY_CAPACITY // 2)
    # Day 1 (locked) untouched at full capacity.
    assert raw[0] == DAILY_CAPACITY
    # Prefix sum at day 3 dropped by exactly overflow_qty.
    assert state.capacity_tree.query(3) == 3 * DAILY_CAPACITY - overflow_qty


def test_apply_batch_to_capacity_add_then_remove_round_trips_to_original() -> None:
    """Add + remove of the same order on the same day must restore the
    tree to its starting state — this is the critical invariant the
    two-branch formula is designed to preserve."""
    state = SchedulerState.initial(_BASE)
    before = state.capacity_tree.to_array()

    add_delta = [0] * HORIZON_DAYS
    add_delta[4] = 7000
    apply_batch_to_capacity(state, add_delta)

    remove_delta = [0] * HORIZON_DAYS
    remove_delta[4] = -7000
    apply_batch_to_capacity(state, remove_delta)

    assert state.capacity_tree.to_array() == before


def test_apply_batch_to_capacity_remove_after_spillback_restores_state() -> None:
    """The harder round-trip: add forces spill-back to earlier days, then
    remove must un-spill correctly so raw values return to the initial
    configuration. Validates the negative-branch (min-clamp) formula."""
    state = SchedulerState.initial(_BASE)
    before = state.capacity_tree.to_array()

    # Add 1.5x cap on day 3 — overflows back into days 1 and 2.
    add_qty = DAILY_CAPACITY + (DAILY_CAPACITY // 2)
    add_delta = [0, 0, add_qty] + [0] * (HORIZON_DAYS - 3)
    apply_batch_to_capacity(state, add_delta)

    # Now remove the same demand.
    remove_delta = [0, 0, -add_qty] + [0] * (HORIZON_DAYS - 3)
    apply_batch_to_capacity(state, remove_delta)

    assert state.capacity_tree.to_array() == before


def test_apply_batch_to_capacity_preserves_prefix_sum_invariant() -> None:
    """For any feasible delta, post-apply prefix sum on day i must equal
    pre-apply prefix sum minus the cumulative delta through day i. This
    is the contract the feasibility check relies on."""
    state = SchedulerState.initial(_BASE)
    delta = [0, 5000, 3000, 0, 7000] + [0] * (HORIZON_DAYS - 5)

    pre_prefix = [state.capacity_tree.query(i) for i in range(1, HORIZON_DAYS + 1)]
    apply_batch_to_capacity(state, delta)
    post_prefix = [state.capacity_tree.query(i) for i in range(1, HORIZON_DAYS + 1)]

    cumulative = 0
    for i in range(HORIZON_DAYS):
        cumulative += delta[i]
        assert post_prefix[i] == pre_prefix[i] - cumulative


def test_apply_batch_to_capacity_wrong_length_raises() -> None:
    """API contract."""
    state = SchedulerState.initial(_BASE)
    import pytest

    with pytest.raises(ValueError, match=str(HORIZON_DAYS)):
        apply_batch_to_capacity(state, [0] * (HORIZON_DAYS - 1))


# ---------------------------------------------------------------------------
# Batch admission — apply_batch_to_deadline
# ---------------------------------------------------------------------------


def test_apply_batch_to_deadline_adds_to_corresponding_day() -> None:
    """Non-zero delta on day i adds delta[i-1] to deadline_tree's day i
    raw value; zero days untouched. Symmetric for negative delta."""
    state = SchedulerState.initial(_BASE)
    delta = [0] * HORIZON_DAYS
    delta[2] = 800  # day 3
    delta[7] = -300  # day 8

    apply_batch_to_deadline(state, delta)

    raw = state.deadline_tree.to_array()
    assert raw[2] == 800
    assert raw[7] == -300
    assert all(r == 0 for i, r in enumerate(raw) if i not in (2, 7))


def test_apply_batch_to_deadline_zero_delta_is_noop() -> None:
    """All-zero delta leaves deadline_tree bit-identical (no point_update
    calls with delta=0 due to the explicit skip)."""
    state = SchedulerState.initial(_BASE)
    before = state.deadline_tree.to_array()

    apply_batch_to_deadline(state, [0] * HORIZON_DAYS)

    assert state.deadline_tree.to_array() == before


def test_apply_batch_to_deadline_wrong_length_raises() -> None:
    """API contract."""
    state = SchedulerState.initial(_BASE)
    import pytest

    with pytest.raises(ValueError, match=str(HORIZON_DAYS)):
        apply_batch_to_deadline(state, [0] * (HORIZON_DAYS + 1))


# ---------------------------------------------------------------------------
# Batch admission — end-to-end equivalence with per-op add_order/remove_order
# ---------------------------------------------------------------------------


def test_batch_apply_matches_per_op_add_for_single_order_within_capacity() -> None:
    """Applying batch updates for a single add op should leave capacity
    and deadline trees in the same prefix-sum state as calling
    ``add_order`` directly on that order. (Raw distributions can differ
    — carry-back fills latest day first whereas ``_apply_add_to_trees``
    backward-fills from prefix-geq; both are valid EDF-equivalent
    distributions that produce identical prefix sums.)
    """
    order = _make_order(qty=2000, deadline=_BASE + timedelta(days=3))

    # Path A: existing per-op add.
    state_a = SchedulerState.initial(_BASE)
    add_order(state_a, order)

    # Path B: batch.
    state_b = SchedulerState.initial(_BASE)
    delta = compute_batch_capacity_delta(
        [BatchOp(kind="add", wafer_quantity=order.wafer_quantity, deadline=order.deadline)],
        _BASE,
    )
    assert is_batch_feasible(state_b, delta) is True
    apply_batch_to_capacity(state_b, delta)
    apply_batch_to_deadline(state_b, delta)

    # Prefix sums match on every day.
    for i in range(1, HORIZON_DAYS + 1):
        assert state_a.capacity_tree.query(i) == state_b.capacity_tree.query(i)
        assert state_a.deadline_tree.query(i) == state_b.deadline_tree.query(i)


def test_batch_apply_matches_per_op_for_mixed_batch() -> None:
    """Same equivalence for a mixed add+remove batch (PATCH-style):
    prefix sums on both trees must match the result of doing each op
    individually via remove_order + add_order."""
    base = _BASE
    # Seed an order so it can be removed.
    seed = _make_order(qty=1500, deadline=base + timedelta(days=5))

    # Path A: per-op.
    state_a = SchedulerState.initial(base)
    add_order(state_a, seed)
    # Replace seed (remove + add at new deadline) and add a brand-new order.
    new_seed = _make_order(qty=1500, deadline=base + timedelta(days=10))
    new_seed = SchedulingOrder(
        order_id=seed.order_id,
        order_number=seed.order_number,
        wafer_quantity=seed.wafer_quantity,
        deadline=base + timedelta(days=10),
    )
    fresh = _make_order(qty=800, deadline=base + timedelta(days=7))
    remove_order(state_a, seed)
    add_order(state_a, new_seed)
    add_order(state_a, fresh)

    # Path B: batch on the same start state.
    state_b = SchedulerState.initial(base)
    add_order(state_b, seed)  # seed shared with path A's pre-state
    batch_ops = [
        BatchOp(kind="remove", wafer_quantity=seed.wafer_quantity, deadline=seed.deadline),
        BatchOp(
            kind="add",
            wafer_quantity=new_seed.wafer_quantity,
            deadline=new_seed.deadline,
        ),
        BatchOp(kind="add", wafer_quantity=fresh.wafer_quantity, deadline=fresh.deadline),
    ]
    delta = compute_batch_capacity_delta(batch_ops, base)
    assert is_batch_feasible(state_b, delta) is True
    apply_batch_to_capacity(state_b, delta)
    apply_batch_to_deadline(state_b, delta)

    for i in range(1, HORIZON_DAYS + 1):
        assert state_a.capacity_tree.query(i) == state_b.capacity_tree.query(i), (
            f"capacity prefix mismatch at day {i}"
        )
        assert state_a.deadline_tree.query(i) == state_b.deadline_tree.query(i), (
            f"deadline prefix mismatch at day {i}"
        )


# ---------------------------------------------------------------------------
# _iter_pq_edf_sorted — direct tests of the lazy bucket-sort iterator
# ---------------------------------------------------------------------------
#
# pq is now dict-backed; EDF ordering is materialized at iteration time via
# bucket sort (30 buckets by deadline_rel + within-bucket sort_key). These
# tests cover the helper directly rather than through advance_day /
# compute_schedule so a regression in the iterator surfaces here, not as a
# downstream symptom.


def test_iter_pq_edf_sorted_empty_pq_returns_empty_list() -> None:
    """Short-circuit branch: no pq orders ⇒ no bucket allocation, return
    ``[]`` directly. Without this guard the function would still allocate
    HORIZON_DAYS+1 empty lists and walk them — correct but wasteful."""
    state = SchedulerState.initial(_BASE)

    assert _iter_pq_edf_sorted(state) == []


def test_iter_pq_edf_sorted_single_order_round_trip() -> None:
    """One order in pq ⇒ helper returns ``[order]``. Sanity check the
    single-bucket-single-entry path."""
    state = SchedulerState.initial(_BASE)
    order = _make_order(qty=1000, deadline=_BASE + timedelta(days=5))
    add_order(state, order)

    result = _iter_pq_edf_sorted(state)

    assert len(result) == 1
    assert result[0].order_id == order.order_id


def test_iter_pq_edf_sorted_orders_by_deadline_first() -> None:
    """Primary sort key is deadline (earlier → first). Verify across multiple
    buckets so the bucket-placement step is exercised."""
    state = SchedulerState.initial(_BASE)
    late = _make_order(order_number="late", qty=500, deadline=_BASE + timedelta(days=15))
    early = _make_order(order_number="early", qty=500, deadline=_BASE + timedelta(days=2))
    mid = _make_order(order_number="mid", qty=500, deadline=_BASE + timedelta(days=8))
    for o in (late, early, mid):  # insert in non-EDF order to confirm bucket sort works
        add_order(state, o)

    result = _iter_pq_edf_sorted(state)

    assert [o.order_id for o in result] == [early.order_id, mid.order_id, late.order_id]


def test_iter_pq_edf_sorted_tie_breaks_by_qty_desc_then_order_number() -> None:
    """Same deadline → tie-break on ``(-wafer_quantity, order_number)``.
    Larger qty wins, alphabetical for equal qty. Locks the in-bucket sort
    contract."""
    state = SchedulerState.initial(_BASE)
    same_day = _BASE + timedelta(days=4)
    small_b = _make_order(order_number="b-small", qty=100, deadline=same_day)
    big_z = _make_order(order_number="z-big", qty=1000, deadline=same_day)
    big_a = _make_order(order_number="a-big", qty=1000, deadline=same_day)
    for o in (small_b, big_z, big_a):
        add_order(state, o)

    result = _iter_pq_edf_sorted(state)
    order_numbers = [o.order_number for o in result]

    # qty=1000 group sorts before qty=100; within qty=1000, "a-big" < "z-big".
    assert order_numbers == ["a-big", "z-big", "b-small"]


def test_iter_pq_edf_sorted_day_1_and_day_30_both_placed_correctly() -> None:
    """Boundary buckets: ``_iter_pq_edf_sorted`` must bucket orders at
    the tree's day-1 and day-30 extremes (= base_date + 1 and base_date +
    30 under the new convention). After the bucket-collision fix day-30
    orders still land in bucket[29], not bucket[30] (the out-of-horizon
    sentinel).
    """
    state = SchedulerState.initial(_BASE)
    day_30 = _make_order(qty=100, deadline=_BASE + timedelta(days=HORIZON_DAYS))
    day_1 = _make_order(qty=100, deadline=_BASE + timedelta(days=1))
    add_order(state, day_30)
    add_order(state, day_1)

    result = _iter_pq_edf_sorted(state)

    assert [o.order_id for o in result] == [day_1.order_id, day_30.order_id]


def test_iter_pq_edf_sorted_parity_with_sorted_baseline() -> None:
    """Property-style: a random-ish bag of orders sorted by ``_iter_pq_edf_sorted``
    must equal ``sorted(..., key=sort_key)``. Pins the helper against a trivial
    reference implementation.

    Deadlines start at ``base + 1`` so every order passes the new admission
    rule (``rel >= 2``); the (i * 3) % (HORIZON_DAYS - 1) spread still
    covers a varied set of buckets to exercise tie-breaks on each axis.
    """
    state = SchedulerState.initial(_BASE)
    orders = []
    # Mix qtys, deadlines, and order_numbers so tie-breaks fire on each axis.
    for i in range(15):
        o = _make_order(
            order_number=f"ord-{i:02d}",
            qty=100 * ((i % 5) + 1),
            deadline=_BASE + timedelta(days=1 + (i * 3) % (HORIZON_DAYS - 1)),
        )
        add_order(state, o)
        orders.append(o)

    result = _iter_pq_edf_sorted(state)
    expected = sorted(orders, key=lambda o: o.sort_key())

    assert [o.order_id for o in result] == [o.order_id for o in expected]


def test_iter_pq_edf_sorted_out_of_horizon_lands_in_sentinel_bucket() -> None:
    """An order whose deadline is past the horizon (defensive — shouldn't
    happen under normal admission control) must NOT collide with real
    day-HORIZON_DAYS orders. Verifies the bucket-collision fix: stale
    orders go into the dedicated sentinel bucket appended after all
    in-horizon buckets, so iteration order is ``[in-horizon orders] +
    [out-of-horizon orders]``."""
    state = SchedulerState.initial(_BASE)
    # Inject an out-of-horizon order directly into pq_index, bypassing
    # add_order which would reject it as ``deadline_too_far``. This is
    # the only way to simulate the "stale order somehow in pq" scenario
    # the sentinel bucket defends against.
    stale = _make_order(
        order_number="stale",
        qty=500,
        deadline=_BASE + timedelta(days=HORIZON_DAYS + 5),
    )
    state.pq_index[stale.order_id] = stale

    in_horizon = _make_order(
        order_number="in-horizon",
        qty=500,
        deadline=_BASE + timedelta(days=HORIZON_DAYS - 1),  # day-30 (rel=30)
    )
    add_order(state, in_horizon)

    result = _iter_pq_edf_sorted(state)

    # Day-30 order comes first (bucket[29]), out-of-horizon last (bucket[30]).
    assert [o.order_id for o in result] == [in_horizon.order_id, stale.order_id]
