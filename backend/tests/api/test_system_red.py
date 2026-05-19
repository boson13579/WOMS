"""Tests for ``GET /api/v1/system/red`` — RED-metrics aggregation endpoint.

The endpoint reads the ``metrics:requests`` Redis ZSET (written by the
middleware in ``app/core/red_metrics.py``) and rolls it up into per-window
aggregates: rate, error percent, latency P50/P95/P99/max, and a per-endpoint
top-10 list.

Tests pre-seed the ZSET directly so the assertions are deterministic and
isolated from the middleware writer — we already cover the writer in
``tests/core/test_red_metrics.py``. The two sides share the ZSET schema
(compact JSON keys ``p``/``m``/``s``/``d``/``t``), so a contract drift in
either direction would be caught immediately.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import bcrypt
import pytest
from app.core.red_metrics import METRICS_KEY, _get_metrics_redis
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_metrics_redis_cache(redis_client: Redis) -> Iterator[None]:
    """Bust the middleware's cached Redis client between tests.

    Same rationale as ``tests/core/test_red_metrics.py``: the LRU cache
    pinned the placeholder URL at import time; clearing it lets the
    service module reconnect to the test container.
    """
    _get_metrics_redis.cache_clear()
    yield
    _get_metrics_redis.cache_clear()


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


def _seed_sample(
    redis_client: Redis,
    *,
    path: str = "GET /api/v1/orders",
    method: str = "GET",
    template: str = "/api/v1/orders",
    status: int = 200,
    duration_ms: float = 10.0,
    ts_ms: int | None = None,
) -> int:
    """Write one RED sample to the ZSET with the same encoding the middleware uses.

    The middleware uses compact field names (``p``/``m``/``s``/``d``/``t``)
    — tests use the same so the service module reads them correctly. The
    ``path`` argument is the full ``METHOD TEMPLATE`` label (matches what
    the service synthesises in ``_endpoint_label``); the separate ``template``
    is what the middleware stores in ``p``.

    Returns the timestamp used.
    """
    del path  # legacy parameter — the real key is `template`.
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    member = json.dumps(
        {"p": template, "m": method, "s": status, "d": duration_ms, "t": ts_ms},
        separators=(",", ":"),
    )
    redis_client.zadd(METRICS_KEY, {member: ts_ms})
    return ts_ms


def _seed_many(
    redis_client: Redis,
    *,
    count: int,
    template: str = "/api/v1/orders",
    method: str = "GET",
    status_codes: list[int] | None = None,
    durations_ms: list[float] | None = None,
    base_ts_ms: int | None = None,
) -> None:
    """Bulk-seed *count* samples, optionally cycling through statuses/durations.

    Each sample is offset by 1 ms so they don't share ZSET members. Use
    *base_ts_ms* to control window inclusion (pass a value < now-window_ms
    to seed samples that should fall outside the window).
    """
    if base_ts_ms is None:
        base_ts_ms = int(time.time() * 1000) - count - 1
    if status_codes is None:
        status_codes = [200]
    if durations_ms is None:
        durations_ms = [10.0]
    for i in range(count):
        status = status_codes[i % len(status_codes)]
        duration = durations_ms[i % len(durations_ms)]
        _seed_sample(
            redis_client,
            template=template,
            method=method,
            status=status,
            duration_ms=duration,
            ts_ms=base_ts_ms + i,
        )


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_red_requires_authentication(client: TestClient) -> None:
    """Unauthenticated → 401 via the unified error envelope."""
    res = client.get("/api/v1/system/red")
    assert res.status_code == 401


def test_red_rbac(client: TestClient, db_session: Session) -> None:
    """viewer / order_manager → 403; scheduler + root → 200.

    RED data is operator-grade; viewers and order managers do not see it.
    """
    _make_user(db_session, username="red_viewer", role=UserRole.viewer)
    _make_user(db_session, username="red_om", role=UserRole.order_manager)
    _make_user(db_session, username="red_sched", role=UserRole.scheduler)
    _make_user(db_session, username="red_root", role=UserRole.root)

    assert (
        client.get("/api/v1/system/red", headers=_auth(_login(client, "red_viewer"))).status_code
        == 403
    )
    assert (
        client.get("/api/v1/system/red", headers=_auth(_login(client, "red_om"))).status_code == 403
    )
    assert (
        client.get("/api/v1/system/red", headers=_auth(_login(client, "red_sched"))).status_code
        == 200
    )
    assert (
        client.get("/api/v1/system/red", headers=_auth(_login(client, "red_root"))).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Empty window edge case
# ---------------------------------------------------------------------------


def test_red_returns_zero_for_empty_window(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """No traffic in the window → zeros all the way down, NOT 404.

    The frontend prefers a stable envelope so it can render "0 req/s"
    without a special "empty data" code path.
    """
    _make_user(db_session, username="red_empty", role=UserRole.scheduler)
    token = _login(client, "red_empty")
    # Wipe any login samples just landed.
    redis_client.delete(METRICS_KEY)

    res = client.get("/api/v1/system/red?window_seconds=60", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "window_seconds": 60,
        "total_requests": 0,
        "rate_per_sec": 0.0,
        "error_count": 0,
        "error_pct": 0.0,
        "latency_ms": {"p50": 0, "p95": 0, "p99": 0, "max": 0},
        "by_endpoint": [],
    }


# ---------------------------------------------------------------------------
# Aggregation math
# ---------------------------------------------------------------------------


def test_red_aggregates_recent_requests(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """100 seeded samples → rate ≈ samples/window, P50/P95 percentiles correct.

    Seeds 100 samples with linearly-increasing durations (1..100 ms) over
    a 60-second window. Expected percentiles:

      * P50 ≈ ceil(0.50 * 100) = 50th value → 50.0
      * P95 ≈ ceil(0.95 * 100) = 95th value → 95.0
      * P99 ≈ ceil(0.99 * 100) = 99th value → 99.0
      * max → 100.0
    """
    _make_user(db_session, username="red_agg", role=UserRole.scheduler)
    token = _login(client, "red_agg")
    redis_client.delete(METRICS_KEY)

    durations = [float(i + 1) for i in range(100)]
    _seed_many(
        redis_client,
        count=100,
        durations_ms=durations,
        # base_ts_ms just inside the 60s window — samples are 1ms apart so
        # we add (count-1) ms to keep the latest sample close to "now".
        base_ts_ms=int(time.time() * 1000) - 100,
    )

    res = client.get("/api/v1/system/red?window_seconds=60", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["total_requests"] == 100
    # rate = 100 / 60 ≈ 1.67
    assert body["rate_per_sec"] == pytest.approx(1.67, abs=0.01)
    # All 200 → no errors.
    assert body["error_count"] == 0
    assert body["error_pct"] == 0.0
    # Percentiles (nearest-rank): 50/95/99/max.
    assert body["latency_ms"]["p50"] == 50
    assert body["latency_ms"]["p95"] == 95
    assert body["latency_ms"]["p99"] == 99
    assert body["latency_ms"]["max"] == 100


def test_red_error_pct_calculation(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """Error rule: 5xx + 4xx that are NOT 401/403/404.

    Seeds 100 samples with the following status mix:

      * 80 x 200  -> not an error
      * 5  x 401  -> excluded (auth flow, not a service error)
      * 5  x 404  -> excluded (caller mis-typed path, not a service error)
      * 5  x 422  -> ERROR (validation failure counts)
      * 5  x 500  -> ERROR (all 5xx count)

    Total errors = 10. error_pct = 10 / 100 * 100 = 10.0.
    """
    _make_user(db_session, username="red_err", role=UserRole.scheduler)
    token = _login(client, "red_err")
    redis_client.delete(METRICS_KEY)

    base = int(time.time() * 1000) - 200
    for i in range(80):
        _seed_sample(redis_client, status=200, ts_ms=base + i)
    for i in range(5):
        _seed_sample(redis_client, status=401, ts_ms=base + 80 + i)
    for i in range(5):
        _seed_sample(redis_client, status=404, ts_ms=base + 85 + i)
    for i in range(5):
        _seed_sample(redis_client, status=422, ts_ms=base + 90 + i)
    for i in range(5):
        _seed_sample(redis_client, status=500, ts_ms=base + 95 + i)

    res = client.get("/api/v1/system/red?window_seconds=60", headers=_auth(token))
    body = res.json()
    assert body["total_requests"] == 100
    # 422 + 500 → 10 errors. 401 / 404 do NOT count.
    assert body["error_count"] == 10
    assert body["error_pct"] == 10.0


def test_red_by_endpoint_top_10(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """20 distinct endpoints → response trims to the top 10 by count.

    Endpoint i is seeded with (i+1) samples so the count ordering is
    deterministic. The top-10 slice should be endpoint 20 down through
    endpoint 11 (20 samples down to 11 samples).
    """
    _make_user(db_session, username="red_top", role=UserRole.scheduler)
    token = _login(client, "red_top")
    redis_client.delete(METRICS_KEY)

    base = int(time.time() * 1000) - 1000
    cursor = 0
    for endpoint_idx in range(1, 21):  # endpoints 1..20
        for _ in range(endpoint_idx):
            _seed_sample(
                redis_client,
                template=f"/api/v1/endpoint_{endpoint_idx:02d}",
                ts_ms=base + cursor,
            )
            cursor += 1

    res = client.get("/api/v1/system/red?window_seconds=60", headers=_auth(token))
    body = res.json()
    by_endpoint = body["by_endpoint"]
    assert len(by_endpoint) == 10
    # Sorted DESC by count: endpoint_20 (count 20) first, endpoint_11 (count 11) last.
    counts = [e["count"] for e in by_endpoint]
    assert counts == [20, 19, 18, 17, 16, 15, 14, 13, 12, 11]
    # Endpoints are recorded as "METHOD PATH" labels.
    assert by_endpoint[0]["endpoint"] == "GET /api/v1/endpoint_20"
    assert by_endpoint[-1]["endpoint"] == "GET /api/v1/endpoint_11"


# ---------------------------------------------------------------------------
# Window bounds
# ---------------------------------------------------------------------------


def test_red_window_seconds_lower_bound(client: TestClient, db_session: Session) -> None:
    """``window_seconds < 1`` is rejected by Query validation (422)."""
    _make_user(db_session, username="red_bounds_low", role=UserRole.scheduler)
    token = _login(client, "red_bounds_low")
    res = client.get("/api/v1/system/red?window_seconds=0", headers=_auth(token))
    assert res.status_code == 422


def test_red_window_seconds_above_retention_still_accepted(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """``window_seconds > 300`` is silently accepted (returns what's available).

    The underlying ZSET is trimmed to the last 1 hour by the middleware
    (widened from 5 min so the ``/system/slo`` endpoint surfaces a more
    representative slice), so very long windows still see only the
    samples physically present. Per Plan A1: do NOT reject — return
    what's there. This protects the frontend from a 422 on perfectly
    valid "show me 10 min of data" calls.
    """
    _make_user(db_session, username="red_bounds_high", role=UserRole.scheduler)
    token = _login(client, "red_bounds_high")
    redis_client.delete(METRICS_KEY)
    # Seed a single recent sample so the response has SOMETHING to roll up.
    _seed_sample(redis_client, status=200, duration_ms=12.5)
    res = client.get("/api/v1/system/red?window_seconds=600", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["window_seconds"] == 600
    assert body["total_requests"] >= 1
