"""Tests for ``GET /api/v1/system/schedule-lag``.

The endpoint reads the ``metrics:schedule_lag`` Redis ZSET (written by
``compound_finalize.perform_compound_db_action`` on each successful
commit) and rolls it into P50 / P95 / max over a trailing window.

Tests seed the ZSET directly so the assertions are deterministic and
isolated from the producer side — same convention as
``test_system_red.py``. The schema is shared via the constant in
``app.services.schedule_lag``, so a contract drift on either side would
be caught immediately.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import bcrypt
import pytest
from app.models.user import User, UserRole
from app.services.schedule_lag import _LAG_SET_KEY, _redis
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_lag_redis_cache(redis_client: Redis) -> Iterator[None]:
    """Bust the module's cached Redis client between tests.

    Same rationale as the RED tests: the lru_cache pinned an
    out-of-process URL at import time; clearing lets the service module
    reconnect to the test container.
    """
    _redis.cache_clear()
    redis_client.delete(_LAG_SET_KEY)
    yield
    _redis.cache_clear()
    redis_client.delete(_LAG_SET_KEY)


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
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200
    return res.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_lag(
    redis_client: Redis,
    *,
    lag_ms: int,
    ts_ms: int | None = None,
    seq: int = 0,
) -> int:
    """Write one lag sample using the same JSON shape the producer emits."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    member = json.dumps({"ts": ts_ms, "lag": lag_ms, "seq": seq})
    redis_client.zadd(_LAG_SET_KEY, {member: ts_ms})
    return ts_ms


# ---------------------------------------------------------------------------
# RBAC + validation
# ---------------------------------------------------------------------------


def test_lag_requires_authentication(client: TestClient) -> None:
    res = client.get("/api/v1/system/schedule-lag")
    assert res.status_code == 401


def test_lag_rbac(client: TestClient, db_session: Session) -> None:
    """viewer / order_manager → 403; scheduler + root → 200."""
    _make_user(db_session, username="lag_viewer", role=UserRole.viewer)
    _make_user(db_session, username="lag_om", role=UserRole.order_manager)
    _make_user(db_session, username="lag_sched", role=UserRole.scheduler)
    _make_user(db_session, username="lag_root", role=UserRole.root)
    for username, expected in (
        ("lag_viewer", 403),
        ("lag_om", 403),
        ("lag_sched", 200),
        ("lag_root", 200),
    ):
        token = _login(client, username)
        assert (
            client.get("/api/v1/system/schedule-lag", headers=_auth(token)).status_code == expected
        )


def test_lag_window_validation_lower_bound(client: TestClient, db_session: Session) -> None:
    """``window_seconds < 1`` → 422."""
    _make_user(db_session, username="lag_v_lo", role=UserRole.scheduler)
    token = _login(client, "lag_v_lo")
    res = client.get("/api/v1/system/schedule-lag?window_seconds=0", headers=_auth(token))
    assert res.status_code == 422


def test_lag_window_validation_upper_bound(client: TestClient, db_session: Session) -> None:
    """``window_seconds > 3600`` → 422. 3600 itself is accepted (the widest pill)."""
    _make_user(db_session, username="lag_v_hi", role=UserRole.scheduler)
    token = _login(client, "lag_v_hi")
    assert (
        client.get(
            "/api/v1/system/schedule-lag?window_seconds=3600",
            headers=_auth(token),
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/system/schedule-lag?window_seconds=3601",
            headers=_auth(token),
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_lag_empty_window_returns_zero_envelope(
    client: TestClient, db_session: Session
) -> None:
    """No samples in window → zeros + ``data_status='ok'`` (NOT degraded)."""
    _make_user(db_session, username="lag_empty", role=UserRole.scheduler)
    token = _login(client, "lag_empty")
    res = client.get("/api/v1/system/schedule-lag?window_seconds=60", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "window_seconds": 60,
        "sample_count": 0,
        "p50_ms": 0,
        "p95_ms": 0,
        "max_ms": 0,
        "data_status": "ok",
    }


def test_lag_percentiles_match_nearest_rank(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """Seed 10 lag samples 10..100 ms and verify P50 / P95 / max.

    Nearest-rank with ``ceil(pct/100 * n)``:
        n = 10  → P50 rank = 5 → 50 ms ; P95 rank = ceil(9.5) = 10 → 100 ms.
    """
    _make_user(db_session, username="lag_pct", role=UserRole.scheduler)
    token = _login(client, "lag_pct")
    now_ms = int(time.time() * 1000)
    for i, lag in enumerate([10, 20, 30, 40, 50, 60, 70, 80, 90, 100]):
        _seed_lag(redis_client, lag_ms=lag, ts_ms=now_ms - 1000 - i, seq=i)
    res = client.get("/api/v1/system/schedule-lag?window_seconds=60", headers=_auth(token))
    body = res.json()
    assert body["sample_count"] == 10
    assert body["p50_ms"] == 50
    assert body["p95_ms"] == 100
    assert body["max_ms"] == 100
    assert body["data_status"] == "ok"


def test_lag_filters_by_window(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """Samples older than ``window_seconds`` must be excluded.

    Trim happens on every write, but the read also filters via
    ``ZRANGEBYSCORE`` — so even before the next write trims the old
    sample, it shouldn't appear in the count.
    """
    _make_user(db_session, username="lag_win", role=UserRole.scheduler)
    token = _login(client, "lag_win")
    now_ms = int(time.time() * 1000)
    # In window (10s back)
    _seed_lag(redis_client, lag_ms=42, ts_ms=now_ms - 10_000, seq=1)
    # Out of window (200s back, window is 60s)
    _seed_lag(redis_client, lag_ms=9_999, ts_ms=now_ms - 200_000, seq=2)
    res = client.get("/api/v1/system/schedule-lag?window_seconds=60", headers=_auth(token))
    body = res.json()
    assert body["sample_count"] == 1
    assert body["max_ms"] == 42


def test_lag_skips_corrupt_samples(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """A member that isn't decodable JSON / missing ``lag`` is silently dropped."""
    _make_user(db_session, username="lag_bad", role=UserRole.scheduler)
    token = _login(client, "lag_bad")
    now_ms = int(time.time() * 1000)
    # Good sample
    _seed_lag(redis_client, lag_ms=33, ts_ms=now_ms - 1000, seq=1)
    # Garbage member at a valid score so it's returned by ZRANGEBYSCORE
    redis_client.zadd(_LAG_SET_KEY, {"not-json": now_ms - 500})
    # Valid JSON missing "lag" key
    redis_client.zadd(
        _LAG_SET_KEY,
        {json.dumps({"ts": now_ms - 600, "seq": 9}): now_ms - 600},
    )
    res = client.get("/api/v1/system/schedule-lag?window_seconds=60", headers=_auth(token))
    body = res.json()
    assert body["sample_count"] == 1
    assert body["max_ms"] == 33
