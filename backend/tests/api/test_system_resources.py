"""Tests for ``GET /api/v1/system/resources`` — USE resources endpoint.

Operator-grade snapshot of DB pool / Redis / Celery for the observability
page (Plan A2). Unlike ``/system/health`` (which any logged-in user can hit
to render the dashboard's Service Health card), this endpoint is gated to
scheduler + root because its values are deep-internal infrastructure
metrics rather than UX status.

The endpoint's design contract:

* Every section in the response is independently nullable. If Redis is down
  the response is ``{"db_pool": {...}, "redis": null, "celery": {...}}``
  with HTTP 200 — the dashboard hides the missing card rather than blowing
  up the whole page.
* ``celery.workers`` is **always an array** when ``celery`` is non-null. An
  empty Celery fleet yields ``workers: []``, not ``None``, so the frontend
  can iterate without a null-check.
* Workers list is capped at 50 entries with a ``truncated`` flag. Astronomical
  fleet sizes are unlikely in this course but the cap is defensive.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import bcrypt
import pytest
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers (mirror tests/api/test_system.py)
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    username: str,
    password: str = "password123",
    role: UserRole = UserRole.viewer,
    email: str | None = None,
) -> User:
    user = User(
        username=username,
        email=email or f"{username}@test.internal",
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


def _make_fake_redis(
    *,
    used_memory: int = 1_048_576,
    used_memory_peak: int = 2_097_152,
    connected_clients: int = 3,
    ops_per_sec: int = 12,
    evicted_keys: int = 0,
    llen: int = 0,
) -> MagicMock:
    """Build a MagicMock that emulates redis-py's ``info(section)`` shape.

    Each ``info(...)`` section returns a dict keyed by the same fields the
    real server returns. We expose only the keys our service reads — extra
    keys would still work but make the fixture noisier.
    """
    fake = MagicMock()

    def _info(section: str) -> dict[str, Any]:
        if section == "memory":
            return {
                "used_memory": used_memory,
                "used_memory_peak": used_memory_peak,
            }
        if section == "clients":
            return {"connected_clients": connected_clients}
        if section == "stats":
            return {
                "instantaneous_ops_per_sec": ops_per_sec,
                "evicted_keys": evicted_keys,
            }
        return {}

    fake.info.side_effect = _info
    fake.llen.return_value = llen
    return fake


# ---------------------------------------------------------------------------
# RBAC gating — scheduler + root only
# ---------------------------------------------------------------------------


def test_resources_requires_authentication(client: TestClient) -> None:
    """Unauthenticated → 401 via the unified error envelope."""
    res = client.get("/api/v1/system/resources")
    assert res.status_code == 401


def test_resources_rbac_scheduler_or_root(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Viewer / order_manager → 403; scheduler + root → 200.

    Resources expose pool sizes / Celery internals; we don't want viewers
    seeing what amounts to an ops dashboard.
    """
    # Keep the probes deterministic so the role check is the only variable.
    monkeypatch.setattr("app.services.system._get_celery_stats", lambda: None)
    monkeypatch.setattr("app.services.system._redis_port_open", lambda: False)

    _make_user(db_session, username="res_viewer", role=UserRole.viewer)
    _make_user(db_session, username="res_om", role=UserRole.order_manager)
    _make_user(db_session, username="res_sched", role=UserRole.scheduler)
    _make_user(db_session, username="res_root", role=UserRole.root)

    viewer_token = _login(client, "res_viewer")
    om_token = _login(client, "res_om")
    sched_token = _login(client, "res_sched")
    root_token = _login(client, "res_root")

    assert client.get("/api/v1/system/resources", headers=_auth(viewer_token)).status_code == 403
    assert client.get("/api/v1/system/resources", headers=_auth(om_token)).status_code == 403
    assert client.get("/api/v1/system/resources", headers=_auth(sched_token)).status_code == 200
    assert client.get("/api/v1/system/resources", headers=_auth(root_token)).status_code == 200


# ---------------------------------------------------------------------------
# DB pool stats
# ---------------------------------------------------------------------------


def test_resources_returns_db_pool_stats(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB pool section is populated and ``utilization_pct`` matches the
    expected formula ``checked_out / (size + max_overflow) * 100``.

    Patches ``get_pool_stats`` at its import site in ``services/system``
    so we can pin deterministic numbers without involving the real pool.
    """
    _make_user(db_session, username="res_db", role=UserRole.scheduler)
    token = _login(client, "res_db")

    monkeypatch.setattr(
        "app.services.system.get_pool_stats",
        lambda: {
            "size": 5,
            "checked_out": 5,
            "overflow": 0,
            "max_overflow": 10,
            "utilization_pct": 33.3,
        },
    )
    # Quiet the other sections so this test focuses on db_pool only.
    monkeypatch.setattr("app.services.system._redis_port_open", lambda: False)
    monkeypatch.setattr("app.services.system._get_celery_stats", lambda: None)

    res = client.get("/api/v1/system/resources", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["db_pool"] is not None
    db_pool = body["db_pool"]
    assert db_pool["size"] == 5
    assert db_pool["checked_out"] == 5
    assert db_pool["overflow"] == 0
    assert db_pool["max_overflow"] == 10
    # 5 / (5 + 10) * 100 = 33.3
    assert db_pool["utilization_pct"] == 33.3


def test_resources_db_pool_null_when_pool_unsupported(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NullPool`` and similar pools without ``.size()`` → ``db_pool: None``.

    ``get_pool_stats`` returns ``None`` in this case; the response is still
    200 and other sections still populate.
    """
    _make_user(db_session, username="res_nullpool", role=UserRole.scheduler)
    token = _login(client, "res_nullpool")

    monkeypatch.setattr("app.services.system.get_pool_stats", lambda: None)
    monkeypatch.setattr("app.services.system._redis_port_open", lambda: False)
    monkeypatch.setattr("app.services.system._get_celery_stats", lambda: None)

    res = client.get("/api/v1/system/resources", headers=_auth(token))
    assert res.status_code == 200
    assert res.json()["db_pool"] is None


# ---------------------------------------------------------------------------
# Redis info
# ---------------------------------------------------------------------------


def test_resources_returns_redis_info(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis section is built from ``info('memory' / 'clients' / 'stats')``.

    Mocks the Redis client to return known fixture values and asserts the
    DTO carries them through unchanged. Real Redis returns ints for all of
    these keys — the service coerces defensively but no coercion should
    fire on the happy path.
    """
    _make_user(db_session, username="res_redis", role=UserRole.scheduler)
    token = _login(client, "res_redis")

    fake_redis = _make_fake_redis(
        used_memory=4_194_304,
        used_memory_peak=8_388_608,
        connected_clients=7,
        ops_per_sec=42,
        evicted_keys=3,
    )
    monkeypatch.setattr("app.services.system._redis_port_open", lambda: True)
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)
    monkeypatch.setattr("app.services.system._get_celery_stats", lambda: None)

    res = client.get("/api/v1/system/resources", headers=_auth(token))
    assert res.status_code == 200
    redis_section = res.json()["redis"]
    assert redis_section == {
        "used_memory_bytes": 4_194_304,
        "used_memory_peak_bytes": 8_388_608,
        "connected_clients": 7,
        "ops_per_sec": 42,
        "evicted_keys": 3,
    }


# ---------------------------------------------------------------------------
# Celery introspection — aggregate counts + per-worker breakdown
# ---------------------------------------------------------------------------


class _FakeInspect:
    """Mimic the subset of Celery's ``Inspect`` we use.

    The real ``celery_app.control.inspect(timeout=...)`` returns an
    ``Inspect`` instance with ``.active()`` / ``.ping()`` etc. methods.
    Tests inject this fake by monkeypatching the entire ``celery_app``
    so the lazy import inside ``_get_celery_stats`` resolves to our stub.
    """

    def __init__(self, active: dict[str, Any] | None, ping: list[Any] | None) -> None:
        self._active = active
        self._ping = ping

    def active(self) -> dict[str, Any] | None:
        return self._active

    def ping(self) -> list[Any] | None:
        return self._ping


class _FakeControl:
    def __init__(self, inspector: _FakeInspect) -> None:
        self._inspector = inspector

    def inspect(self, timeout: float = 0.5) -> _FakeInspect:
        del timeout
        return self._inspector


class _FakeCeleryApp:
    def __init__(self, inspector: _FakeInspect) -> None:
        self.control = _FakeControl(inspector)


def _patch_celery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active: dict[str, Any] | None,
    ping: list[Any] | None,
) -> None:
    """Install a fake celery_app at the service's import site.

    ``app.services.system`` imports ``celery_app`` at module level, so we
    patch the symbol where it's bound — not on the source module — so the
    service code resolves through our fake.
    """
    fake_app = _FakeCeleryApp(_FakeInspect(active=active, ping=ping))
    monkeypatch.setattr("app.services.system.celery_app", fake_app)


def test_resources_returns_celery_aggregate(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 mocked workers (2 active, 1 idle) → aggregate counts match.

    ``active_tasks = 2 + 1 + 0 = 3``,
    ``registered_workers = 3`` (the union of hostnames in active+ping).
    """
    _make_user(db_session, username="res_cel_agg", role=UserRole.scheduler)
    token = _login(client, "res_cel_agg")

    active = {
        "celery@w1": [{"id": "t1"}, {"id": "t2"}],  # 2 active tasks
        "celery@w2": [{"id": "t3"}],  # 1 active task
        "celery@w3": [],  # idle
    }
    ping = [
        {"celery@w1": {"ok": "pong"}},
        {"celery@w2": {"ok": "pong"}},
        {"celery@w3": {"ok": "pong"}},
    ]
    _patch_celery(monkeypatch, active=active, ping=ping)

    fake_redis = _make_fake_redis(llen=7)
    monkeypatch.setattr("app.services.system._redis_port_open", lambda: True)
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)
    monkeypatch.setattr("app.services.system.get_pool_stats", lambda: None)

    res = client.get("/api/v1/system/resources", headers=_auth(token))
    assert res.status_code == 200
    celery_section = res.json()["celery"]
    assert celery_section is not None
    assert celery_section["active_tasks"] == 3
    assert celery_section["queue_depth"] == 7
    assert celery_section["registered_workers"] == 3
    assert celery_section["truncated"] is False


def test_resources_includes_per_worker_breakdown(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-worker rows are sorted by hostname with correct status + count.

    Same 3-worker fixture as ``test_resources_returns_celery_aggregate``
    but here we assert the ``workers[]`` array shape: length 3, sorted by
    hostname, statuses ``["active","active","idle"]``, counts matching.
    """
    _make_user(db_session, username="res_cel_per", role=UserRole.scheduler)
    token = _login(client, "res_cel_per")

    active = {
        "celery@w2": [{"id": "t3"}],  # deliberately out-of-order
        "celery@w1": [{"id": "t1"}, {"id": "t2"}],
        "celery@w3": [],
    }
    ping = [
        {"celery@w1": {"ok": "pong"}},
        {"celery@w2": {"ok": "pong"}},
        {"celery@w3": {"ok": "pong"}},
    ]
    _patch_celery(monkeypatch, active=active, ping=ping)

    fake_redis = _make_fake_redis(llen=0)
    monkeypatch.setattr("app.services.system._redis_port_open", lambda: True)
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)
    monkeypatch.setattr("app.services.system.get_pool_stats", lambda: None)

    res = client.get("/api/v1/system/resources", headers=_auth(token))
    assert res.status_code == 200
    workers = res.json()["celery"]["workers"]
    assert isinstance(workers, list)
    assert len(workers) == 3
    # Sorted by hostname ascending — w1, w2, w3.
    hostnames = [w["hostname"] for w in workers]
    assert hostnames == ["celery@w1", "celery@w2", "celery@w3"]
    statuses = [w["status"] for w in workers]
    assert statuses == ["active", "active", "idle"]
    counts = [w["active_tasks"] for w in workers]
    assert counts == [2, 1, 0]


def test_resources_workers_empty_when_no_celery(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``inspect()`` returns ``None`` (no workers) → ``workers: []``, not raise.

    The celery section is still non-null (we have a signal: zero workers).
    Aggregate fields are all zero, ``workers`` is an empty array, and
    ``truncated`` is ``False``.
    """
    _make_user(db_session, username="res_cel_empty", role=UserRole.scheduler)
    token = _login(client, "res_cel_empty")

    _patch_celery(monkeypatch, active=None, ping=None)

    fake_redis = _make_fake_redis(llen=0)
    monkeypatch.setattr("app.services.system._redis_port_open", lambda: True)
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)
    monkeypatch.setattr("app.services.system.get_pool_stats", lambda: None)

    res = client.get("/api/v1/system/resources", headers=_auth(token))
    assert res.status_code == 200
    celery_section = res.json()["celery"]
    assert celery_section is not None
    assert celery_section["workers"] == []
    assert celery_section["active_tasks"] == 0
    assert celery_section["registered_workers"] == 0
    assert celery_section["truncated"] is False


def test_resources_workers_truncated_at_50(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """60 workers → ``workers.length == 50, truncated: True``.

    Defensive cap on the per-worker array so a wildly oversized fleet
    can't balloon the JSON payload. The aggregate ``active_tasks`` /
    ``registered_workers`` reflect the FULL count, not the truncated
    slice — those numbers tell operators "you're past the cap".
    """
    _make_user(db_session, username="res_cel_trunc", role=UserRole.scheduler)
    token = _login(client, "res_cel_trunc")

    # 60 idle workers; hostnames zero-padded for deterministic sort.
    active: dict[str, list[Any]] = {f"celery@w{idx:02d}": [] for idx in range(60)}
    ping = [{f"celery@w{idx:02d}": {"ok": "pong"}} for idx in range(60)]
    _patch_celery(monkeypatch, active=active, ping=ping)

    fake_redis = _make_fake_redis(llen=0)
    monkeypatch.setattr("app.services.system._redis_port_open", lambda: True)
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)
    monkeypatch.setattr("app.services.system.get_pool_stats", lambda: None)

    res = client.get("/api/v1/system/resources", headers=_auth(token))
    assert res.status_code == 200
    celery_section = res.json()["celery"]
    assert celery_section is not None
    assert len(celery_section["workers"]) == 50
    assert celery_section["truncated"] is True
    # Aggregate numbers reflect the true fleet size, not the slice.
    assert celery_section["registered_workers"] == 60
    # First and last hostnames in the sorted slice are w00 and w49.
    assert celery_section["workers"][0]["hostname"] == "celery@w00"
    assert celery_section["workers"][-1]["hostname"] == "celery@w49"


# ---------------------------------------------------------------------------
# Partial failure semantics — one bad probe doesn't drag the others down
# ---------------------------------------------------------------------------


def test_resources_partial_failure_returns_null_section(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis blows up → ``redis: None`` but ``db_pool`` and ``celery`` populate.

    Pins the per-section isolation: one dead dependency must NOT 500 the
    endpoint or null out the surviving sections.
    """
    _make_user(db_session, username="res_partial", role=UserRole.scheduler)
    token = _login(client, "res_partial")

    # Redis client raises on info() — simulate "Redis port open but server hung".
    fake_redis = MagicMock()
    fake_redis.info.side_effect = RuntimeError("redis info hung")
    monkeypatch.setattr("app.services.system._redis_port_open", lambda: True)
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)

    monkeypatch.setattr(
        "app.services.system.get_pool_stats",
        lambda: {
            "size": 5,
            "checked_out": 1,
            "overflow": 0,
            "max_overflow": 10,
            "utilization_pct": 6.7,
        },
    )
    # Celery populated separately (independent of Redis info())
    _patch_celery(monkeypatch, active={}, ping=[])

    res = client.get("/api/v1/system/resources", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["redis"] is None  # the dead section
    assert body["db_pool"] is not None  # surviving sections still present
    assert body["celery"] is not None


def test_resources_celery_failure_returns_null_celery(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``inspect()`` raises → ``celery: None`` and other sections still populate.

    Mirror of the Redis partial-failure case for the Celery probe.
    """
    _make_user(db_session, username="res_cel_fail", role=UserRole.scheduler)
    token = _login(client, "res_cel_fail")

    class _ExplodingControl:
        def inspect(self, timeout: float = 0.5) -> Any:
            del timeout
            raise RuntimeError("celery broker unreachable")

    class _ExplodingApp:
        control = _ExplodingControl()

    monkeypatch.setattr("app.services.system.celery_app", _ExplodingApp())

    fake_redis = _make_fake_redis()
    monkeypatch.setattr("app.services.system._redis_port_open", lambda: True)
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(
        "app.services.system.get_pool_stats",
        lambda: {
            "size": 5,
            "checked_out": 0,
            "overflow": 0,
            "max_overflow": 10,
            "utilization_pct": 0.0,
        },
    )

    res = client.get("/api/v1/system/resources", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["celery"] is None
    assert body["redis"] is not None
    assert body["db_pool"] is not None
