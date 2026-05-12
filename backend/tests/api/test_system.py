"""Tests for the ``/api/v1/system/*`` HTTP router.

The endpoint backs the dashboard's Service Health card. Unlike the existing
``/api/v1/health`` liveness probe (which always returns 200/ok so k8s can
decide whether to restart the pod), ``/system/health`` actually probes the
DB / Redis / Celery state and reports per-service status so a human can see
at a glance whether the cluster is degraded.

Authn is required (any logged-in user — viewers included — can read), but
the underlying probes can fail in interesting ways, so most of the assertions
focus on shape + per-service ``status`` values across mock probe outcomes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import bcrypt
import pytest
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers
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


# ---------------------------------------------------------------------------
# [RED] auth gates
# ---------------------------------------------------------------------------


def test_system_health_requires_authentication(client: TestClient) -> None:
    """Unauthenticated request gets a 401 via the unified error envelope."""
    res = client.get("/api/v1/system/health")
    assert res.status_code == 401
    body = res.json()
    assert "error" in body
    assert body["error"]["code"] == 401


def test_system_health_open_to_viewer(client: TestClient, db_session: Session) -> None:
    """Service Health is visible to any logged-in role, including viewer.

    Dashboard widget choice (per plan): viewers see Service Health even
    though they cannot see scheduling data. Confirms we do NOT add a role
    gate on this endpoint.
    """
    _make_user(db_session, username="sys_viewer", role=UserRole.viewer)
    token = _login(client, "sys_viewer")
    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# [RED] response shape — pins the contract the frontend depends on
# ---------------------------------------------------------------------------


def test_system_health_returns_four_services(client: TestClient, db_session: Session) -> None:
    """Response shape: a single ``services`` array containing 4 entries
    (api / postgres / redis / celery) in a fixed order so the frontend
    can index by position if it ever needs to."""
    _make_user(db_session, username="sys_shape", role=UserRole.viewer)
    token = _login(client, "sys_shape")
    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert "services" in body
    services = body["services"]
    assert isinstance(services, list)
    assert len(services) == 4
    ids = [s["id"] for s in services]
    assert ids == ["api", "postgres", "redis", "celery"]


def test_system_health_each_service_has_required_fields(
    client: TestClient, db_session: Session
) -> None:
    """Every service entry carries id / name / status / summary / details."""
    _make_user(db_session, username="sys_fields", role=UserRole.viewer)
    token = _login(client, "sys_fields")
    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200
    services = res.json()["services"]
    for entry in services:
        assert set(entry.keys()) >= {"id", "name", "status", "summary", "details"}
        assert entry["status"] in {"healthy", "warning", "error"}
        assert isinstance(entry["details"], list)
        for detail in entry["details"]:
            assert set(detail.keys()) == {"label", "value"}


def test_system_health_api_service_always_healthy(client: TestClient, db_session: Session) -> None:
    """API service status is computed locally — if we can answer the request
    at all, by definition the API is up.

    Pins the invariant so a future refactor can't accidentally introduce
    a probe that ends up calling ``self`` recursively.
    """
    _make_user(db_session, username="sys_api", role=UserRole.viewer)
    token = _login(client, "sys_api")
    res = client.get("/api/v1/system/health", headers=_auth(token))
    api_entry = next(s for s in res.json()["services"] if s["id"] == "api")
    assert api_entry["status"] == "healthy"


# ---------------------------------------------------------------------------
# [RED] probe-outcome branches — postgres / redis / celery via service mocks
# ---------------------------------------------------------------------------


def test_system_health_postgres_error_when_probe_raises(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the Postgres probe raises, its entry reports ``error`` and the
    overall request still succeeds (degraded mode — one bad service doesn't
    fail the whole dashboard)."""
    _make_user(db_session, username="sys_pg_err", role=UserRole.viewer)
    token = _login(client, "sys_pg_err")

    def _broken_probe(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr("app.services.system._probe_postgres", _broken_probe)
    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200
    pg_entry = next(s for s in res.json()["services"] if s["id"] == "postgres")
    assert pg_entry["status"] == "error"


def test_system_health_redis_error_when_probe_raises(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the Postgres case for Redis."""
    _make_user(db_session, username="sys_redis_err", role=UserRole.viewer)
    token = _login(client, "sys_redis_err")

    def _broken_probe() -> None:
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.services.system._probe_redis", _broken_probe)
    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200
    redis_entry = next(s for s in res.json()["services"] if s["id"] == "redis")
    assert redis_entry["status"] == "error"


def test_system_health_celery_healthy_when_queue_deep_but_draining(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deep queue alone is NOT a warning signal. Bombard / burst workflows
    legitimately push hundreds of compounds in seconds — what matters is
    whether the worker is actually draining them, which the stall detector
    (below) covers. Pins the design decision so a future refactor doesn't
    re-add a queue-depth warning rule without revisiting it.
    """
    from datetime import UTC, datetime

    _make_user(db_session, username="sys_cel_deep", role=UserRole.viewer)
    token = _login(client, "sys_cel_deep")

    just_now = datetime.now(tz=UTC).isoformat()
    fake_redis = MagicMock()
    fake_redis.zcard.return_value = 999  # very deep
    # state=idle but finished_at is fresh → worker is alive between tasks.
    fake_redis.get.return_value = f'{{"state": "idle", "finished_at": "{just_now}"}}'
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)

    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200
    celery_entry = next(s for s in res.json()["services"] if s["id"] == "celery")
    assert celery_entry["status"] == "healthy"


def test_system_health_celery_warning_when_status_failed(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``schedule:status.state == 'failed'`` → celery warning, regardless
    of queue depth. The dashboard's primary value here is making this
    visible without an ops-side log dive."""
    _make_user(db_session, username="sys_cel_failed", role=UserRole.viewer)
    token = _login(client, "sys_cel_failed")

    fake_redis = MagicMock()
    fake_redis.zcard.return_value = 0
    fake_redis.get.return_value = '{"state": "failed", "error": "boom"}'
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)

    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200
    celery_entry = next(s for s in res.json()["services"] if s["id"] == "celery")
    assert celery_entry["status"] == "warning"


def test_system_health_celery_healthy_when_idle_empty_queue(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline: status=idle, queue=0 → healthy."""
    _make_user(db_session, username="sys_cel_ok", role=UserRole.viewer)
    token = _login(client, "sys_cel_ok")

    fake_redis = MagicMock()
    fake_redis.zcard.return_value = 0
    fake_redis.get.return_value = '{"state": "idle"}'
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)

    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200
    celery_entry = next(s for s in res.json()["services"] if s["id"] == "celery")
    assert celery_entry["status"] == "healthy"


def test_system_health_celery_error_when_redis_unreachable(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Celery probe goes through Redis to read ``schedule:status`` /
    ``schedule:pending_ops``. If Redis itself raises, celery's probe can't
    answer — surface as ``error`` so the dashboard makes it clear we have
    no information about the worker, not a false ``healthy``.
    """
    _make_user(db_session, username="sys_cel_err", role=UserRole.viewer)
    token = _login(client, "sys_cel_err")

    fake_redis = MagicMock()
    fake_redis.zcard.side_effect = RuntimeError("no redis")
    fake_redis.get.side_effect = RuntimeError("no redis")
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)

    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200
    celery_entry = next(s for s in res.json()["services"] if s["id"] == "celery")
    assert celery_entry["status"] == "error"


def test_system_health_celery_warning_when_idle_with_stale_queue(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker dead but queue still has items + idle status + stale finish
    → warning. Pins the "stall detection" rule the dashboard relies on
    (frontend ``deriveScheduleDisplay`` uses the same 30s threshold)."""
    from datetime import UTC, datetime, timedelta

    _make_user(db_session, username="sys_cel_stall", role=UserRole.viewer)
    token = _login(client, "sys_cel_stall")

    long_ago = (datetime.now(tz=UTC) - timedelta(seconds=120)).isoformat()
    fake_redis = MagicMock()
    fake_redis.zcard.return_value = 5  # queue non-empty but ≤ 50 threshold
    fake_redis.get.return_value = f'{{"state": "idle", "finished_at": "{long_ago}"}}'
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)

    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200
    celery_entry = next(s for s in res.json()["services"] if s["id"] == "celery")
    assert celery_entry["status"] == "warning"
    assert "stuck" in celery_entry["summary"].lower()


def test_system_health_celery_healthy_when_idle_with_fresh_finish(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same setup but ``finished_at`` is fresh → still healthy (we're just
    in the between-task gap, worker is alive)."""
    from datetime import UTC, datetime, timedelta

    _make_user(db_session, username="sys_cel_fresh", role=UserRole.viewer)
    token = _login(client, "sys_cel_fresh")

    just_now = (datetime.now(tz=UTC) - timedelta(seconds=2)).isoformat()
    fake_redis = MagicMock()
    fake_redis.zcard.return_value = 5
    fake_redis.get.return_value = f'{{"state": "idle", "finished_at": "{just_now}"}}'
    monkeypatch.setattr("app.services.system._get_redis_client", lambda: fake_redis)

    res = client.get("/api/v1/system/health", headers=_auth(token))
    assert res.status_code == 200
    celery_entry = next(s for s in res.json()["services"] if s["id"] == "celery")
    assert celery_entry["status"] == "healthy"


# ===========================================================================
# GET /system/usernames — bulk UUID → username lookup
# ===========================================================================
#
# The dashboard's Pending Ops table needs to render the requester's username
# next to each compound. ``/users`` is root-only by design (full CRUD), so a
# slimmer read-only "give me the username for these UUIDs" endpoint lives
# here in ``/system/*`` and is open to any logged-in user. Same RBAC bar as
# ``/system/health``.


def test_system_usernames_requires_authentication(client: TestClient) -> None:
    """Unauthenticated → 401 via the unified error envelope."""
    res = client.get("/api/v1/system/usernames?ids=0e6ff691-0923-47c7-81b4-d5cf6359b004")
    assert res.status_code == 401


def test_system_usernames_returns_username_map(client: TestClient, db_session: Session) -> None:
    """Happy path: pass a list of UUIDs, get back {uuid: username}."""
    alice = _make_user(db_session, username="lookup_alice", role=UserRole.scheduler)
    bob = _make_user(db_session, username="lookup_bob", role=UserRole.viewer)
    viewer = _make_user(db_session, username="lookup_viewer", role=UserRole.viewer)
    token = _login(client, "lookup_viewer")

    res = client.get(
        f"/api/v1/system/usernames?ids={alice.id},{bob.id}",
        headers=_auth(token),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["usernames"] == {
        str(alice.id): "lookup_alice",
        str(bob.id): "lookup_bob",
    }
    # Sanity: we did NOT request viewer's id, so it must not be in the map.
    assert str(viewer.id) not in data["usernames"]


def test_system_usernames_returns_null_for_unknown_uuid(
    client: TestClient, db_session: Session
) -> None:
    """Unknown UUIDs map to ``null`` — caller can distinguish missing rows
    from a 4xx error, and partial results don't block the table render."""
    import uuid as _uuid

    _make_user(db_session, username="lookup_partial", role=UserRole.viewer)
    token = _login(client, "lookup_partial")

    unknown = _uuid.uuid4()
    res = client.get(
        f"/api/v1/system/usernames?ids={unknown}",
        headers=_auth(token),
    )
    assert res.status_code == 200
    assert res.json()["usernames"] == {str(unknown): None}


def test_system_usernames_dedupes_repeated_uuids(client: TestClient, db_session: Session) -> None:
    """If a UUID appears twice in ``ids``, it appears once in the response.

    The frontend's typical usage is "collect distinct requested_by from
    pending-ops table"; dedup-on-receive saves the caller from sending a
    set conversion every time.
    """
    user = _make_user(db_session, username="lookup_dup", role=UserRole.viewer)
    token = _login(client, "lookup_dup")

    res = client.get(
        f"/api/v1/system/usernames?ids={user.id},{user.id}",
        headers=_auth(token),
    )
    assert res.status_code == 200
    assert res.json()["usernames"] == {str(user.id): "lookup_dup"}


def test_system_usernames_rejects_empty_ids(client: TestClient, db_session: Session) -> None:
    """``?ids=`` (empty string) is a programming error on the caller side;
    a 422 makes the bug surface immediately rather than silently returning
    ``{}``."""
    _make_user(db_session, username="lookup_empty", role=UserRole.viewer)
    token = _login(client, "lookup_empty")

    res = client.get("/api/v1/system/usernames?ids=", headers=_auth(token))
    assert res.status_code == 422


def test_system_usernames_caps_request_size(client: TestClient, db_session: Session) -> None:
    """A huge ``ids`` list is an abuse / programming-error signal — cap at
    100 per request and 422 anything larger. Frontend chunks requests if
    it ever needs more (it never does today)."""
    import uuid as _uuid

    _make_user(db_session, username="lookup_cap", role=UserRole.viewer)
    token = _login(client, "lookup_cap")

    too_many = ",".join(str(_uuid.uuid4()) for _ in range(101))
    res = client.get(f"/api/v1/system/usernames?ids={too_many}", headers=_auth(token))
    assert res.status_code == 422


def test_system_usernames_rejects_malformed_uuid(client: TestClient, db_session: Session) -> None:
    """Garbage in the ``ids`` query → 422 with the unified error envelope.

    Don't try to silently skip — caller should fix the bug at the source.
    """
    _make_user(db_session, username="lookup_bad_uuid", role=UserRole.viewer)
    token = _login(client, "lookup_bad_uuid")

    res = client.get("/api/v1/system/usernames?ids=not-a-uuid", headers=_auth(token))
    assert res.status_code == 422
