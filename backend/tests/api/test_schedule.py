"""Tests for the ``/api/v1/schedule/*`` HTTP router.

Uses the project's `client` fixture (real Postgres via testcontainers) for
auth + DB, but mocks Redis and the Celery ``.delay()`` so no broker is
needed.
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
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers (module-level per CLAUDE.md test convention)
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal in-memory stand-in for the keys the router touches.

    The router writes pending ops to a sorted set (``ZADD`` keyed by
    ``score_for_op``) and reads ``schedule:state`` / ``schedule:status``
    via plain string ops.
    """

    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._zsets: dict[str, list[tuple[float, str]]] = {}

    # ----- String ops --------------------------------------------------------
    def get(self, key: str) -> str | None:
        return self._strings.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        # ``ex`` (TTL) accepted for API compat; tests don't actually expire.
        self._strings[key] = value

    def incr(self, key: str) -> int:
        cur = int(self._strings.get(key, "0")) + 1
        self._strings[key] = str(cur)
        return cur

    def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            if self._strings.pop(key, None) is not None:
                n += 1
            if self._zsets.pop(key, None) is not None:
                n += 1
        return n

    # ----- Sorted-set ops ----------------------------------------------------
    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        bucket = self._zsets.setdefault(key, [])
        added = 0
        for member, score in mapping.items():
            existing = next((i for i, (_, m) in enumerate(bucket) if m == member), None)
            if existing is not None:
                bucket.pop(existing)
            else:
                added += 1
            insert_at = next(
                (i for i, (s, m) in enumerate(bucket) if (s, m) > (score, member)),
                len(bucket),
            )
            bucket.insert(insert_at, (score, member))
        return added

    def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, []))

    def zrange(self, key: str, start: int, stop: int) -> list[str]:
        bucket = self._zsets.get(key, [])
        # Redis ZRANGE: stop is inclusive; -1 means "last element".
        sliced = bucket[start:] if stop == -1 else bucket[start : stop + 1]
        return [member for _, member in sliced]


def _make_user(
    db: Session,
    *,
    username: str,
    password: str = "password123",
    role: UserRole = UserRole.viewer,
) -> User:
    user = User(
        username=username,
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


def _patch_redis_and_delay(monkeypatch, fake_redis: _FakeRedis) -> MagicMock:
    """Swap both modules' Redis clients + Celery .delay; return the delay mock.

    The api router caches its own Redis client and ``services.schedule_queue``
    has a separate ``_redis`` for ``enqueue_compound`` / ``list_pending_ops`` —
    both need to point at the same in-memory fake so a write via one path is
    visible to a read via the other.
    """
    monkeypatch.setattr("app.api.v1.schedule._redis", lambda: fake_redis)
    monkeypatch.setattr("app.services.schedule_queue._redis", lambda: fake_redis)
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
    fake_redis = _FakeRedis()
    delay_mock = _patch_redis_and_delay(monkeypatch, fake_redis)
    _make_user(db_session, username="sched_trig_ok", role=UserRole.scheduler)
    token = _login(client, "sched_trig_ok")

    res = client.post("/api/v1/schedule/trigger", headers=_auth(token))

    assert res.status_code == 202
    body = res.json()
    assert body["task_id"] == "task-mock"
    assert body["message"] == "Scheduling started"
    assert delay_mock.called


def test_trigger_returns_409_when_already_running(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    fake_redis = _FakeRedis()
    fake_redis.set("schedule:status", json.dumps({"state": "running"}))
    delay_mock = _patch_redis_and_delay(monkeypatch, fake_redis)
    _make_user(db_session, username="sched_trig_dup", role=UserRole.scheduler)
    token = _login(client, "sched_trig_dup")

    res = client.post("/api/v1/schedule/trigger", headers=_auth(token))

    assert res.status_code == 409
    assert res.json()["error"]["code"] == 409
    assert not delay_mock.called


def test_trigger_by_viewer_returns_403(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
# POST /operations
# ---------------------------------------------------------------------------


def test_operations_enqueues_compound(
    client: TestClient,
    db_session: Session,
    _autouse_mock_enqueue_compound: MagicMock,
) -> None:
    """Endpoint accepts compound shape and forwards to schedule_queue.

    The autouse fixture has replaced ``enqueue_compound`` with a MagicMock,
    so we don't need a fake Redis here — just verify the endpoint reached
    the helper with the right payload.
    """
    _make_user(db_session, username="sched_op_idle", role=UserRole.scheduler)
    token = _login(client, "sched_op_idle")

    res = client.post(
        "/api/v1/schedule/operations",
        headers=_auth(token),
        json=_VALID_COMPOUND_PAYLOAD,
    )

    assert res.status_code == 202
    body = res.json()
    assert body["message"] == "Compound queued"
    assert "compound_id" in body
    # enqueue_compound was called exactly once with the parsed compound.
    assert _autouse_mock_enqueue_compound.call_count == 1
    enqueued = _autouse_mock_enqueue_compound.call_args.args[0]
    assert enqueued.group == "grow"
    assert len(enqueued.ops) == 1
    assert enqueued.ops[0].op == "add"


def test_operations_rejects_empty_ops(client: TestClient, db_session: Session) -> None:
    """The compound schema requires at least 1 op (``min_length=1``). Sending
    an empty ``ops`` list trips a standard pydantic validation error — the
    422 unified-error contract is what tests should observe.
    """
    _make_user(db_session, username="sched_op_empty", role=UserRole.scheduler)
    token = _login(client, "sched_op_empty")

    payload = {
        "group": "grow",
        "op_count": 0,
        "requested_by": str(uuid.uuid4()),
        "ops": [],
    }

    res = client.post(
        "/api/v1/schedule/operations",
        headers=_auth(token),
        json=payload,
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == 422


def test_operations_rejects_op_count_mismatch(
    client: TestClient, db_session: Session
) -> None:
    """``op_count`` MUST equal ``len(ops)``. Sending a wrong count triggers
    the schema-level tamper guard, before any Redis interaction.
    """
    _make_user(db_session, username="sched_op_count", role=UserRole.scheduler)
    token = _login(client, "sched_op_count")

    payload = {
        "group": "grow",
        "op_count": 5,  # lies — only 1 op below
        "requested_by": str(uuid.uuid4()),
        "ops": [
            {
                "op": "add",
                "order_id": str(uuid.uuid4()),
                "order_number": "ORD-COUNT",
                "wafer_quantity": 100,
                "deadline": "2026-08-01",
            }
        ],
    }

    res = client.post(
        "/api/v1/schedule/operations",
        headers=_auth(token),
        json=payload,
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == 422


def test_operations_by_viewer_returns_403(client: TestClient, db_session: Session) -> None:
    _make_user(db_session, username="viewer_ops", role=UserRole.viewer)
    token = _login(client, "viewer_ops")

    res = client.post(
        "/api/v1/schedule/operations",
        headers=_auth(token),
        json=_VALID_COMPOUND_PAYLOAD,
    )

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_operations_without_token_returns_401(client: TestClient) -> None:
    res = client.post("/api/v1/schedule/operations", json=_VALID_COMPOUND_PAYLOAD)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401


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


def test_cancel_compound_by_viewer_returns_403(
    client: TestClient, db_session: Session
) -> None:
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
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    fake_redis = _FakeRedis()
    fake_redis.set(
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
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
    _make_user(db_session, username="mgr_status_empty", role=UserRole.order_manager)
    token = _login(client, "mgr_status_empty")

    res = client.get("/api/v1/schedule/status", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "idle"
    assert body["message"] == "No scheduling has been run yet"


def test_status_by_viewer_returns_403(client: TestClient, db_session: Session, monkeypatch) -> None:
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)

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
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
    fake_redis: _FakeRedis,
    *,
    group: str,
    seq: int,
    compound_id: uuid.UUID,
    ops: list[tuple[str, uuid.UUID, str]],
    requested_by: uuid.UUID,
) -> None:
    """Write one ScheduleCompoundRequest directly into the fake Redis sorted set.

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
    fake_redis.zadd("schedule:pending_ops", {json.dumps(payload): score})


def test_pending_ops_returns_compounds_ranked_by_drain_order(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """The endpoint must rank shrink-group compounds before grow-group,
    FIFO within each group — same order ``run_scheduling_task`` pops them.
    """
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
        fake_redis,
        group="grow",
        seq=1,
        compound_id=c_grow,
        ops=[("add", o1, "ORD-GROW")],
        requested_by=actor,
    )
    _enqueue_payload_directly(
        fake_redis,
        group="shrink",
        seq=2,
        compound_id=c_shrink_old,
        ops=[("remove", o2, "ORD-SHRINK-A")],
        requested_by=actor,
    )
    _enqueue_payload_directly(
        fake_redis,
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
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """A compound may legitimately touch >1 order_id (batch business
    actions). ``ops`` in the response keeps per-leaf order info so the
    dashboard can answer "where is order X queued?" even when X shares
    a compound with another order.
    """
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
    _make_user(db_session, username="mgr_pq_multi", role=UserRole.order_manager)
    token = _login(client, "mgr_pq_multi")

    c_multi = uuid.uuid4()
    o_a, o_b = uuid.uuid4(), uuid.uuid4()
    actor = uuid.uuid4()

    _enqueue_payload_directly(
        fake_redis,
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
    a_rank = min(
        it["rank"]
        for it in items
        if any(op["order_id"] == str(o_a) for op in it["ops"])
    )
    b_rank = min(
        it["rank"]
        for it in items
        if any(op["order_id"] == str(o_b) for op in it["ops"])
    )
    assert a_rank == 1
    assert b_rank == 1


def test_pending_ops_returns_empty_list_when_queue_is_idle(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """No compound enqueued ⇒ 200 with an empty list, not 404 or 500."""
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
    _make_user(db_session, username="mgr_pq_empty", role=UserRole.order_manager)
    token = _login(client, "mgr_pq_empty")

    res = client.get("/api/v1/schedule/pending-ops", headers=_auth(token))

    assert res.status_code == 200
    assert res.json() == []


def test_pending_ops_by_viewer_returns_403(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """GET /capacity reads the live SchedulerState and exposes the
    capacity_tree as a per-day prefix-sum series. Building a known state
    with one 4,000-wafer order on day 1 must produce:
        day 1 → 6,000 (10,000 - 4,000)
        day 2 → 16,000 (day1 + full day2)
        day n → 6,000 + (n - 1) * 10,000
    so any off-by-one in the prefix-sum walk surfaces immediately.
    """
    from app.services.scheduling import (
        DAILY_CAPACITY,
        HORIZON_DAYS,
        SchedulerState,
        SchedulingOrder,
        add_order,
    )

    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
            deadline=base,
        ),
    )
    fake_redis.set("schedule:state", state.to_json())

    res = client.get("/api/v1/schedule/capacity", headers=_auth(token))

    assert res.status_code == 200
    body = res.json()
    assert body["base_date"] == base.isoformat()
    assert body["daily_capacity"] == DAILY_CAPACITY
    assert len(body["entries"]) == HORIZON_DAYS
    # Day 1 lost 4,000 wafers; every subsequent day adds DAILY_CAPACITY.
    assert body["entries"][0] == {
        "date": base.isoformat(),
        "cumulative_remaining": DAILY_CAPACITY - 4_000,
    }
    assert body["entries"][1] == {
        "date": (base + timedelta(days=1)).isoformat(),
        "cumulative_remaining": (DAILY_CAPACITY - 4_000) + DAILY_CAPACITY,
    }
    # Last entry — sanity check the closing of the prefix sum.
    last = body["entries"][-1]
    assert last["date"] == (base + timedelta(days=HORIZON_DAYS - 1)).isoformat()
    assert last["cumulative_remaining"] == (DAILY_CAPACITY - 4_000) + (HORIZON_DAYS - 1) * DAILY_CAPACITY


def test_capacity_without_redis_state_returns_full_horizon(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """No SchedulerState in Redis (first deploy / cache flush) must NOT
    500 or return an empty payload — the dashboard should still get a
    full 30-entry response with capacity = daily_capacity * day_index.
    """
    from app.services.scheduling import DAILY_CAPACITY, HORIZON_DAYS

    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
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
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Rebuild no longer 409s when a scheduling run is in progress — instead
    the task is queued and serializes itself by polling status. This test
    verifies the API layer dispatches unconditionally; the wait-for-idle
    logic is in the task body and tested in the worker suite."""
    fake_redis = _FakeRedis()
    fake_redis.set("schedule:status", json.dumps({"state": "running"}))
    _patch_redis_and_delay(monkeypatch, fake_redis)
    rebuild_delay_mock = _patch_rebuild_delay(monkeypatch)

    _make_user(db_session, username="sched_rebuild_busy", role=UserRole.scheduler)
    token = _login(client, "sched_rebuild_busy")

    res = client.post("/api/v1/schedule/rebuild", headers=_auth(token))

    assert res.status_code == 202
    rebuild_delay_mock.assert_called_once()


def test_rebuild_by_viewer_returns_403(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    fake_redis = _FakeRedis()
    _patch_redis_and_delay(monkeypatch, fake_redis)
    _make_user(db_session, username="viewer_rebuild", role=UserRole.viewer)
    token = _login(client, "viewer_rebuild")

    res = client.post("/api/v1/schedule/rebuild", headers=_auth(token))

    assert res.status_code == 403
    assert res.json()["error"]["code"] == 403


def test_rebuild_without_token_returns_401(client: TestClient) -> None:
    res = client.post("/api/v1/schedule/rebuild")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == 401
