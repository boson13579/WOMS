"""Tests for the ``/api/v1/schedule/*`` HTTP router.

Uses the project's `client` fixture (real Postgres via testcontainers) for
auth + DB, and the session-wide ``redis_container`` from the root conftest
for real Redis behavior. Celery ``.delay()`` is still mocked since there's
no broker / worker pair running in-process — but everything that touches
Redis keys hits the live container.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from unittest.mock import MagicMock

import bcrypt
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers (module-level per CLAUDE.md test convention)
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    username: str,
    password: str = "password123",
    role: UserRole = UserRole.viewer,
) -> User:
    user = User(
        username=username,
        email=f"{username}@test.internal",
        password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client: TestClient, username: str, password: str = "password123") -> str:
    res = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200
    return res.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_order(
    db: Session,
    *,
    created_by: uuid.UUID,
    status: OrderStatus = OrderStatus.pending,
    scheduled_production_date: date | None = None,
    expected_delivery_date: date | None = None,
    customer_name: str = "Test Customer",
    wafer_quantity: int = 100,
    requested_delivery_date: date = date(2026, 8, 1),
) -> Order:
    order = Order(
        order_number=f"ORD-TEST-{uuid.uuid4().hex[:6].upper()}",
        customer_name=customer_name,
        wafer_quantity=wafer_quantity,
        requested_delivery_date=requested_delivery_date,
        scheduled_production_date=scheduled_production_date,
        expected_delivery_date=expected_delivery_date,
        status=status,
        created_by=created_by,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _patch_delay(monkeypatch) -> MagicMock:
    """Stub ``run_scheduling_task.delay`` so tests can assert dispatch without
    needing a Celery broker / worker pair. Returns the mock so tests can
    inspect call args + verify the task id wired into the response.

    Redis itself is NOT patched — the session-scoped ``redis_container``
    fixture exposes a real Redis at the URL the app reads from settings,
    so any ``_redis()`` call from production code reaches it natively.
    """
    delay_mock = MagicMock(return_value=MagicMock(id="task-mock"))
    monkeypatch.setattr("app.api.v1.schedule.run_scheduling_task.delay", delay_mock)
    return delay_mock


_VALID_COMPOUND_PAYLOAD = {
    "group": "grow",
    "op_count": 1,
    "requested_by": str(uuid.uuid4()),
    "ops": [
        {
            "op": "add",
            "order_id": str(uuid.uuid4()),
            "order_number": "ORD-OP-PAYLOAD",
            "wafer_quantity": 100,
            "deadline": "2026-08-01",
        }
    ],
}


# ---------------------------------------------------------------------------
# POST /trigger
# ---------------------------------------------------------------------------


def test_trigger_success_returns_202(client: TestClient, db_session: Session, monkeypatch) -> None:
    delay_mock = _patch_delay(monkeypatch)
    _make_user(db_session, username="sched_trig_ok", role=UserRole.scheduler)
    token = _login(client, "sched_trig_ok")

    res = client.post("/api/v1/schedule/trigger", headers=_auth(token))

    assert res.status_code == 202
    body = res.json()
    assert body["task_id"] == "task-mock"
    assert body["message"] == "Scheduling started"
    assert delay_mock.called


def test_trigger_returns_409_when_already_running(
    client: TestClient, db_session: Session, monkeypatch, redis_client: Redis
) -> None:
    redis_client.set("schedule:status", json.dumps({"state": "running"}))
    delay_mock = _patch_delay(monkeypatch)
    _make_user(db_session, username="sched_trig_dup", role=UserRole.scheduler)
    token = _login(client, "sched_trig_dup")

    res = client.post("/api/v1/schedule/trigger", headers=_auth(token))

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409
    assert not delay_mock.called


def test_trigger_by_viewer_returns_403(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _patch_delay(monkeypatch)
    _make_user(db_session, username="viewer_trig", role=UserRole.viewer)
    token = _login(client, "viewer_trig")

    res = client.post("/api/v1/schedule/trigger", headers=_auth(token))

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_trigger_without_token_returns_401(client: TestClient) -> None:
    res = client.post("/api/v1/schedule/trigger")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# POST /operations — REMOVED
# ---------------------------------------------------------------------------
#
# The raw POST /schedule/operations endpoint was removed in favor of
# folding pin / unpin into PATCH /orders/{id} via the
# pinned_production_date field. See app/api/v1/schedule.py for rationale.
# Pin / unpin compound coverage now lives in tests/services/test_order.py
# (the PATCH-with-pin transitions). DELETE /operations/{compound_id} is a
# separate endpoint and is still tested below.


def test_apply_schedule_audit_row_has_null_user_id_for_system_actor(
    db_session: Session,
) -> None:
    """The ``order.scheduled`` audit row written by ``apply_schedule`` must
    have ``user_id IS NULL`` — scheduling is system-driven, not user-driven,
    so there is no human actor to attribute. Pairs with the parallel check
    in the service-layer test; the verifier called out that this assertion
    was missing at the API/integration tier.
    """
    from datetime import date

    from app.models.audit_log import AuditLog
    from app.services import order as order_service
    from app.services.scheduling import ScheduledResult
    from sqlalchemy import select

    creator = _make_user(db_session, username="sched_audit_sys", role=UserRole.scheduler)
    order = _make_order(
        db_session,
        created_by=creator.id,
        requested_delivery_date=date(2026, 7, 1),
    )

    scheduled = [
        ScheduledResult(order_id=order.id, scheduled_date=date(2026, 6, 15), quantity=100),
    ]
    applied = order_service.apply_schedule(db_session, scheduled)
    assert applied == 1

    row = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "order.scheduled")
        .where(AuditLog.resource_id == order.id)
    ).one()
    # The whole point of this assertion: no forged user id sneaks in for
    # a system-driven write.
    assert row.user_id is None
    assert row.resource_type == "order"


# ---------------------------------------------------------------------------
# DELETE /operations/{compound_id} — cancel
# ---------------------------------------------------------------------------


def test_cancel_compound_200_when_in_queue(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Happy path: compound is still in the queue → 200, cancel_compound
    helper returns ``CancelResult.cancelled``.
    """
    from app.services.schedule_queue import CancelResult

    _make_user(db_session, username="sched_cancel_ok", role=UserRole.scheduler)
    token = _login(client, "sched_cancel_ok")

    cancel_mock = MagicMock(return_value=CancelResult.cancelled)
    monkeypatch.setattr("app.api.v1.schedule.cancel_compound", cancel_mock)

    compound_id = uuid.uuid4()
    res = client.delete(
        f"/api/v1/schedule/operations/{compound_id}",
        headers=_auth(token),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["compound_id"] == str(compound_id)
    assert body["message"] == "Compound cancelled"
    # cancel_compound called with the parsed UUID.
    cancel_mock.assert_called_once_with(compound_id)


def test_cancel_compound_409_when_already_in_progress(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Worker won the race between our HGET and ZREM. Helper returns
    ``CancelResult.in_progress`` → endpoint returns 409.
    """
    from app.services.schedule_queue import CancelResult

    _make_user(db_session, username="sched_cancel_race", role=UserRole.scheduler)
    token = _login(client, "sched_cancel_race")

    monkeypatch.setattr(
        "app.api.v1.schedule.cancel_compound",
        MagicMock(return_value=CancelResult.in_progress),
    )

    compound_id = uuid.uuid4()
    res = client.delete(
        f"/api/v1/schedule/operations/{compound_id}",
        headers=_auth(token),
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409


def test_cancel_compound_404_when_unknown(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Compound id is unknown to the secondary index → 404."""
    from app.services.schedule_queue import CancelResult

    _make_user(db_session, username="sched_cancel_missing", role=UserRole.scheduler)
    token = _login(client, "sched_cancel_missing")

    monkeypatch.setattr(
        "app.api.v1.schedule.cancel_compound",
        MagicMock(return_value=CancelResult.not_found),
    )

    res = client.delete(
        f"/api/v1/schedule/operations/{uuid.uuid4()}",
        headers=_auth(token),
    )

    assert res.status_code == 404
    assert res.json()["error"]["code"] == 404


def test_cancel_compound_by_viewer_returns_403(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="viewer_cancel", role=UserRole.viewer)
    token = _login(client, "viewer_cancel")

    res = client.delete(
        f"/api/v1/schedule/operations/{uuid.uuid4()}",
        headers=_auth(token),
    )

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_cancel_compound_without_token_returns_401(client: TestClient) -> None:
    res = client.delete(f"/api/v1/schedule/operations/{uuid.uuid4()}")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


def test_status_returns_redis_doc_when_present(
    client: TestClient, db_session: Session, monkeypatch, redis_client: Redis
) -> None:
    redis_client.set(
        "schedule:status",
        json.dumps(
            {
                "state": "running",
                "started_at": "2026-05-05T00:00:00+00:00",
                "finished_at": None,
                "task_id": "task-running",
                "error": None,
            }
        ),
    )
    _patch_delay(monkeypatch)
    _make_user(db_session, username="mgr_status_data", role=UserRole.order_manager)
    token = _login(client, "mgr_status_data")

    res = client.get("/api/v1/schedule/status", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "running"
    assert body["task_id"] == "task-running"
    assert body["started_at"] == "2026-05-05T00:00:00+00:00"


def test_status_returns_idle_default_when_empty(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _patch_delay(monkeypatch)
    _make_user(db_session, username="mgr_status_empty", role=UserRole.order_manager)
    token = _login(client, "mgr_status_empty")

    res = client.get("/api/v1/schedule/status", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "idle"
    assert body["message"] == "No scheduling has been run yet"


def test_status_by_viewer_returns_403(client: TestClient, db_session: Session, monkeypatch) -> None:
    _patch_delay(monkeypatch)
    _make_user(db_session, username="viewer_status", role=UserRole.viewer)
    token = _login(client, "viewer_status")

    res = client.get("/api/v1/schedule/status", headers=_auth(token))

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_status_without_token_returns_401(client: TestClient) -> None:
    res = client.get("/api/v1/schedule/status")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# GET /result
# ---------------------------------------------------------------------------


def test_result_returns_scheduled_orders_sorted_by_production_date(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _patch_delay(monkeypatch)
    user = _make_user(db_session, username="mgr_result_ok", role=UserRole.order_manager)
    token = _login(client, "mgr_result_ok")

    later = _make_order(
        db_session,
        created_by=user.id,
        status=OrderStatus.scheduled,
        scheduled_production_date=date(2026, 6, 10),
        expected_delivery_date=date(2026, 6, 12),
    )
    earlier = _make_order(
        db_session,
        created_by=user.id,
        status=OrderStatus.scheduled,
        scheduled_production_date=date(2026, 5, 20),
        expected_delivery_date=date(2026, 5, 22),
    )
    # Excluded: not in scheduled status.
    _make_order(db_session, created_by=user.id, status=OrderStatus.pending)

    res = client.get("/api/v1/schedule/result", headers=_auth(token))

    assert res.status_code == 200
    items = res.json()
    ids = [item["id"] for item in items]
    assert ids == [str(earlier.id), str(later.id)]
    # Each item carries the schedule-relevant fields.
    assert items[0]["scheduled_production_date"] == "2026-05-20"
    assert items[0]["expected_delivery_date"] == "2026-05-22"
    assert items[0]["status"] == "scheduled"
    # daily_breakdown column is NULL ⇒ response falls back to empty list.
    assert items[0]["daily_breakdown"] == []
    assert items[1]["daily_breakdown"] == []


def test_result_includes_daily_breakdown_from_db_column(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """GET /result reads ``daily_breakdown`` straight from the DB column.

    Redis ``SchedulerState`` is no longer consulted on this read path —
    ``materialize_schedule_task`` is responsible for keeping
    ``orders.daily_breakdown`` in sync, so the endpoint just echoes what's
    in Postgres.
    """
    _patch_delay(monkeypatch)

    user = _make_user(db_session, username="mgr_breakdown", role=UserRole.order_manager)
    token = _login(client, "mgr_breakdown")

    base = date(2026, 5, 6)
    next_day = base + timedelta(days=1)
    order = _make_order(
        db_session,
        created_by=user.id,
        status=OrderStatus.scheduled,
        scheduled_production_date=base,
        expected_delivery_date=next_day,
    )
    order.daily_breakdown = [
        {"date": base.isoformat(), "quantity": 10_000},
        {"date": next_day.isoformat(), "quantity": 5_000},
    ]
    db_session.commit()

    res = client.get("/api/v1/schedule/result", headers=_auth(token))

    assert res.status_code == 200
    items = res.json()
    assert len(items) == 1
    assert items[0]["daily_breakdown"] == [
        {"date": base.isoformat(), "quantity": 10_000},
        {"date": next_day.isoformat(), "quantity": 5_000},
    ]


def test_result_excludes_soft_deleted_orders(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _patch_delay(monkeypatch)
    user = _make_user(db_session, username="mgr_result_del", role=UserRole.order_manager)
    token = _login(client, "mgr_result_del")

    deleted = _make_order(
        db_session,
        created_by=user.id,
        status=OrderStatus.scheduled,
        scheduled_production_date=date(2026, 5, 20),
    )
    deleted.is_deleted = True
    db_session.commit()

    res = client.get("/api/v1/schedule/result", headers=_auth(token))

    assert res.status_code == 200
    assert res.json() == []


def test_result_by_viewer_returns_403(client: TestClient, db_session: Session, monkeypatch) -> None:
    _patch_delay(monkeypatch)
    _make_user(db_session, username="viewer_result", role=UserRole.viewer)
    token = _login(client, "viewer_result")

    res = client.get("/api/v1/schedule/result", headers=_auth(token))

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_result_without_token_returns_401(client: TestClient) -> None:
    res = client.get("/api/v1/schedule/result")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# GET /pending-ops
# ---------------------------------------------------------------------------


def _enqueue_payload_directly(
    redis_client: Redis,
    *,
    group: str,
    seq: int,
    compound_id: uuid.UUID,
    ops: list[tuple[str, uuid.UUID, str]],
    requested_by: uuid.UUID,
) -> None:
    """Write one ScheduleCompoundRequest directly into the live Redis sorted set.

    Bypasses ``enqueue_compound`` (which would also fire a Celery .delay)
    so we can construct an exact queue state for the assertion. ``ops`` is
    a list of ``(op_kind, order_id, order_number)`` tuples — a compound
    may legally span multiple order_ids, so each leaf carries its own.
    """
    from app.services.scheduling import score_for_op

    ops_payload = [
        {
            "op": kind,
            "order_id": str(order_id),
            "order_number": order_number,
            "wafer_quantity": 100,
            "deadline": "2026-08-01",
        }
        for kind, order_id, order_number in ops
    ]
    payload = {
        "compound_id": str(compound_id),
        "group": group,
        "op_count": len(ops),
        "ops": ops_payload,
        "requested_by": str(requested_by),
        "_seq": seq,
    }
    score = score_for_op(group=group, seq=seq)
    redis_client.zadd("schedule:pending_ops", {json.dumps(payload): score})


def test_pending_ops_returns_compounds_ranked_by_drain_order(
    client: TestClient, db_session: Session, monkeypatch, redis_client: Redis
) -> None:
    """The endpoint must rank shrink-group compounds before grow-group,
    FIFO within each group — same order ``run_scheduling_task`` pops them.
    """
    _patch_delay(monkeypatch)
    _make_user(db_session, username="mgr_pq_ok", role=UserRole.order_manager)
    token = _login(client, "mgr_pq_ok")

    c_grow = uuid.uuid4()
    c_shrink_old = uuid.uuid4()
    c_shrink_new = uuid.uuid4()
    o1, o2, o3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    actor = uuid.uuid4()

    # Enqueue out-of-order to prove the endpoint sorts (or rather: trusts
    # ZRANGE's natural order). grow goes in first with a low seq, but
    # shrink must still rank above it.
    _enqueue_payload_directly(
        redis_client,
        group="grow",
        seq=1,
        compound_id=c_grow,
        ops=[("add", o1, "ORD-GROW")],
        requested_by=actor,
    )
    _enqueue_payload_directly(
        redis_client,
        group="shrink",
        seq=2,
        compound_id=c_shrink_old,
        ops=[("remove", o2, "ORD-SHRINK-A")],
        requested_by=actor,
    )
    _enqueue_payload_directly(
        redis_client,
        group="shrink",
        seq=3,
        compound_id=c_shrink_new,
        ops=[("unpin", o3, "ORD-SHRINK-B"), ("remove", o3, "ORD-SHRINK-B")],
        requested_by=actor,
    )

    res = client.get("/api/v1/schedule/pending-ops", headers=_auth(token))

    assert res.status_code == 200
    items = res.json()
    assert [it["rank"] for it in items] == [1, 2, 3]
    # Shrink-group compounds come first regardless of seq order; FIFO within
    # group means shrink_old (seq=2) ranks above shrink_new (seq=3).
    assert items[0]["compound_id"] == str(c_shrink_old)
    assert items[0]["group"] == "shrink"
    assert items[0]["ops"] == [
        {"op": "remove", "order_id": str(o2), "order_number": "ORD-SHRINK-A"},
    ]
    assert items[1]["compound_id"] == str(c_shrink_new)
    assert items[1]["op_count"] == 2
    assert [op["op"] for op in items[1]["ops"]] == ["unpin", "remove"]
    # Grow-group ranks last.
    assert items[2]["compound_id"] == str(c_grow)
    assert items[2]["group"] == "grow"


def test_pending_ops_supports_compounds_spanning_multiple_orders(
    client: TestClient, db_session: Session, monkeypatch, redis_client: Redis
) -> None:
    """A compound may legitimately touch >1 order_id (batch business
    actions). ``ops`` in the response keeps per-leaf order info so the
    dashboard can answer "where is order X queued?" even when X shares
    a compound with another order.
    """
    _patch_delay(monkeypatch)
    _make_user(db_session, username="mgr_pq_multi", role=UserRole.order_manager)
    token = _login(client, "mgr_pq_multi")

    c_multi = uuid.uuid4()
    o_a, o_b = uuid.uuid4(), uuid.uuid4()
    actor = uuid.uuid4()

    _enqueue_payload_directly(
        redis_client,
        group="grow",
        seq=1,
        compound_id=c_multi,
        ops=[
            ("remove", o_a, "ORD-BATCH-A"),
            ("add", o_b, "ORD-BATCH-B"),
        ],
        requested_by=actor,
    )

    res = client.get("/api/v1/schedule/pending-ops", headers=_auth(token))

    assert res.status_code == 200
    items = res.json()
    assert len(items) == 1
    assert items[0]["op_count"] == 2
    # Both order_ids show up in the same compound's ops; ranks for both
    # are simply the compound's rank (=1).
    assert {op["order_id"] for op in items[0]["ops"]} == {str(o_a), str(o_b)}
    # The dashboard's per-order lookup pattern: find compounds whose
    # ops contain the order_id of interest.
    a_rank = min(it["rank"] for it in items if any(op["order_id"] == str(o_a) for op in it["ops"]))
    b_rank = min(it["rank"] for it in items if any(op["order_id"] == str(o_b) for op in it["ops"]))
    assert a_rank == 1
    assert b_rank == 1


def test_pending_ops_returns_empty_list_when_queue_is_idle(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """No compound enqueued ⇒ 200 with an empty list, not 404 or 500."""
    _patch_delay(monkeypatch)
    _make_user(db_session, username="mgr_pq_empty", role=UserRole.order_manager)
    token = _login(client, "mgr_pq_empty")

    res = client.get("/api/v1/schedule/pending-ops", headers=_auth(token))

    assert res.status_code == 200
    assert res.json() == []


def test_pending_ops_by_viewer_returns_403(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _patch_delay(monkeypatch)
    _make_user(db_session, username="viewer_pq", role=UserRole.viewer)
    token = _login(client, "viewer_pq")

    res = client.get("/api/v1/schedule/pending-ops", headers=_auth(token))

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_pending_ops_without_token_returns_401(client: TestClient) -> None:
    res = client.get("/api/v1/schedule/pending-ops")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# GET /capacity
# ---------------------------------------------------------------------------


def test_capacity_with_state_returns_prefix_sums(
    client: TestClient, db_session: Session, monkeypatch, redis_client: Redis
) -> None:
    """GET /capacity reads the live SchedulerState and exposes the
    capacity_tree as a per-day prefix-sum series. Tree day 1 = ``base +
    1`` (tomorrow), so ``entries[0].date`` is tomorrow, not today. A
    4,000-wafer order with deadline = tomorrow sits on tree day 1 →
    that day's residual capacity = 6,000.

    Expected prefix sums:
        entries[0] (tomorrow) → 6,000 (10,000 - 4,000)
        entries[1] (tomorrow + 1) → 16,000
        entries[i] → (i + 1) * 10,000 - 4,000
        entries[-1] (base + 30) → 30 * 10,000 - 4,000 = 296,000
    """
    from app.services.scheduling import (
        DAILY_CAPACITY,
        HORIZON_DAYS,
        SchedulerState,
        SchedulingOrder,
        add_order,
    )

    _patch_delay(monkeypatch)
    _make_user(db_session, username="mgr_cap_ok", role=UserRole.order_manager)
    token = _login(client, "mgr_cap_ok")

    base = date(2026, 5, 6)
    state = SchedulerState.initial(base)
    add_order(
        state,
        SchedulingOrder(
            order_id=uuid.uuid4(),
            order_number="ORD-CAP-1",
            wafer_quantity=4_000,
            deadline=base + timedelta(days=1),  # rel = 1 (= tree day 1 = tomorrow)
        ),
    )
    redis_client.set("schedule:state", state.to_json())

    res = client.get("/api/v1/schedule/capacity", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["base_date"] == base.isoformat()
    assert body["daily_capacity"] == DAILY_CAPACITY
    assert len(body["entries"]) == HORIZON_DAYS
    # entries[0] = tomorrow with 4,000 consumed.
    assert body["entries"][0] == {
        "date": (base + timedelta(days=1)).isoformat(),
        "cumulative_remaining": DAILY_CAPACITY - 4_000,
    }
    # entries[1] = day after tomorrow, cumulative adds full day's capacity.
    assert body["entries"][1] == {
        "date": (base + timedelta(days=2)).isoformat(),
        "cumulative_remaining": 2 * DAILY_CAPACITY - 4_000,
    }
    # Last entry = base + HORIZON_DAYS, full horizon prefix sum minus the 4000.
    last = body["entries"][-1]
    assert last["date"] == (base + timedelta(days=HORIZON_DAYS)).isoformat()
    assert last["cumulative_remaining"] == HORIZON_DAYS * DAILY_CAPACITY - 4_000


def test_capacity_without_redis_state_returns_full_horizon(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """No SchedulerState in Redis (first deploy / cache flush) must NOT
    500 or return an empty payload — the dashboard should still get a
    full 30-entry response with capacity = daily_capacity * day_index.
    """
    from app.services.scheduling import DAILY_CAPACITY, HORIZON_DAYS

    _patch_delay(monkeypatch)
    _make_user(db_session, username="mgr_cap_empty", role=UserRole.order_manager)
    token = _login(client, "mgr_cap_empty")

    res = client.get("/api/v1/schedule/capacity", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["daily_capacity"] == DAILY_CAPACITY
    assert len(body["entries"]) == HORIZON_DAYS
    # Every day is empty ⇒ prefix sums are exact multiples of DAILY_CAPACITY.
    for i, entry in enumerate(body["entries"], start=1):
        assert entry["cumulative_remaining"] == i * DAILY_CAPACITY


def test_capacity_by_viewer_returns_403(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _patch_delay(monkeypatch)
    _make_user(db_session, username="viewer_cap", role=UserRole.viewer)
    token = _login(client, "viewer_cap")

    res = client.get("/api/v1/schedule/capacity", headers=_auth(token))

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_capacity_without_token_returns_401(client: TestClient) -> None:
    res = client.get("/api/v1/schedule/capacity")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# GET /capacity-usage
# ---------------------------------------------------------------------------


def test_capacity_usage_returns_realized_per_day_view(
    client: TestClient, db_session: Session, monkeypatch, redis_client: Redis
) -> None:
    """GET /capacity-usage reads the DB snapshot written by ``apply_schedule``
    and aligns it against the SchedulerState's base_date. Two orders sharing
    day 1 should aggregate; days the snapshot doesn't cover come back as
    used=0, remaining=daily_capacity. This verifies the snapshot → endpoint
    pipeline end-to-end with both populated and empty days.
    """
    from app.models.order import Order, OrderStatus
    from app.services import order as order_service
    from app.services.scheduling import (
        DAILY_CAPACITY,
        HORIZON_DAYS,
        ScheduledResult,
        SchedulerState,
    )

    _patch_delay(monkeypatch)
    creator = _make_user(db_session, username="mgr_capuse_ok", role=UserRole.order_manager)
    token = _login(client, "mgr_capuse_ok")

    base = date(2026, 5, 21)
    redis_client.set("schedule:state", SchedulerState.initial(base).to_json())

    # Two orders share day 1 → 700 used; only A on day 2 → 200.
    # ``Order.wafer_quantity`` has a [25, 2500] check constraint; per-day
    # ``ScheduledResult.quantity`` (the aggregation source) is independent
    # and just needs to be plausible.
    order_a = Order(
        order_number="ORD-CU-A",
        customer_name="X",
        wafer_quantity=600,
        requested_delivery_date=date(2026, 5, 25),
        created_by=creator.id,
        status=OrderStatus.pending,
    )
    order_b = Order(
        order_number="ORD-CU-B",
        customer_name="X",
        wafer_quantity=300,
        requested_delivery_date=date(2026, 5, 26),
        created_by=creator.id,
        status=OrderStatus.pending,
    )
    db_session.add_all([order_a, order_b])
    db_session.commit()
    db_session.refresh(order_a)
    db_session.refresh(order_b)

    order_service.apply_schedule(
        db_session,
        [
            ScheduledResult(order_id=order_a.id, scheduled_date=base, quantity=400),
            ScheduledResult(
                order_id=order_a.id, scheduled_date=base + timedelta(days=1), quantity=200
            ),
            ScheduledResult(order_id=order_b.id, scheduled_date=base, quantity=300),
        ],
    )

    res = client.get("/api/v1/schedule/capacity-usage", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["base_date"] == base.isoformat()
    assert body["daily_capacity"] == DAILY_CAPACITY
    assert len(body["entries"]) == HORIZON_DAYS

    # Day 1: a(400) + b(300) = 700 used.
    assert body["entries"][0] == {
        "date": base.isoformat(),
        "used": 700,
        "remaining": DAILY_CAPACITY - 700,
    }
    # Day 2: a(200) only.
    assert body["entries"][1] == {
        "date": (base + timedelta(days=1)).isoformat(),
        "used": 200,
        "remaining": DAILY_CAPACITY - 200,
    }
    # Day 3 onward: nothing scheduled → used=0.
    assert body["entries"][2] == {
        "date": (base + timedelta(days=2)).isoformat(),
        "used": 0,
        "remaining": DAILY_CAPACITY,
    }


def test_capacity_usage_without_redis_state_uses_today_as_base(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """When Redis ``schedule:state`` is missing (fresh deploy or flushed
    cache), the endpoint falls back to today as base_date so the dashboard
    still gets a usable 30-entry response instead of a 500.
    """
    from app.services.scheduling import DAILY_CAPACITY, HORIZON_DAYS

    _patch_delay(monkeypatch)
    _make_user(db_session, username="mgr_capuse_empty", role=UserRole.order_manager)
    token = _login(client, "mgr_capuse_empty")

    res = client.get("/api/v1/schedule/capacity-usage", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["daily_capacity"] == DAILY_CAPACITY
    assert len(body["entries"]) == HORIZON_DAYS
    # No snapshot rows seeded → every day is fully available.
    for entry in body["entries"]:
        assert entry["used"] == 0
        assert entry["remaining"] == DAILY_CAPACITY


def test_capacity_usage_by_viewer_returns_403(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _patch_delay(monkeypatch)
    _make_user(db_session, username="viewer_capuse", role=UserRole.viewer)
    token = _login(client, "viewer_capuse")

    res = client.get("/api/v1/schedule/capacity-usage", headers=_auth(token))

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_capacity_usage_without_token_returns_401(client: TestClient) -> None:
    res = client.get("/api/v1/schedule/capacity-usage")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


# ---------------------------------------------------------------------------
# POST /rebuild
# ---------------------------------------------------------------------------


def _patch_rebuild_delay(monkeypatch) -> MagicMock:
    """Patch ``rebuild_schedule_task.delay`` so the API doesn't enqueue a
    real Celery task. Returns the mock for assertions."""
    rebuild_delay_mock = MagicMock(return_value=MagicMock(id="rebuild-task-mock"))
    monkeypatch.setattr("app.api.v1.schedule.rebuild_schedule_task.delay", rebuild_delay_mock)
    return rebuild_delay_mock


def test_rebuild_returns_202_and_dispatches_task(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Happy path: rebuild dispatches ``rebuild_schedule_task`` and returns
    202 with the new task id. The actual rebuild work happens inside the
    task body (covered in ``tests/workers/test_scheduling_task.py``)."""
    _patch_delay(monkeypatch)
    rebuild_delay_mock = _patch_rebuild_delay(monkeypatch)

    _make_user(db_session, username="sched_rebuild_ok", role=UserRole.scheduler)
    token = _login(client, "sched_rebuild_ok")

    res = client.post("/api/v1/schedule/rebuild", headers=_auth(token))

    assert res.status_code == 202
    body = res.json()
    assert body["task_id"] == "rebuild-task-mock"
    assert "queued" in body["message"].lower()
    rebuild_delay_mock.assert_called_once()


def test_rebuild_dispatches_even_when_run_scheduling_is_running(
    client: TestClient, db_session: Session, monkeypatch, redis_client: Redis
) -> None:
    """Rebuild no longer 409s when a scheduling run is in progress — instead
    the task is queued and serializes itself by polling status. This test
    verifies the API layer dispatches unconditionally; the wait-for-idle
    logic is in the task body and tested in the worker suite."""
    redis_client.set("schedule:status", json.dumps({"state": "running"}))
    _patch_delay(monkeypatch)
    rebuild_delay_mock = _patch_rebuild_delay(monkeypatch)

    _make_user(db_session, username="sched_rebuild_busy", role=UserRole.scheduler)
    token = _login(client, "sched_rebuild_busy")

    res = client.post("/api/v1/schedule/rebuild", headers=_auth(token))

    assert res.status_code == 202
    rebuild_delay_mock.assert_called_once()


def test_rebuild_by_viewer_returns_403(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _patch_delay(monkeypatch)
    _make_user(db_session, username="viewer_rebuild", role=UserRole.viewer)
    token = _login(client, "viewer_rebuild")

    res = client.post("/api/v1/schedule/rebuild", headers=_auth(token))

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_rebuild_without_token_returns_401(client: TestClient) -> None:
    res = client.post("/api/v1/schedule/rebuild")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401
