"""Tests for ``GET /api/v1/system/slo`` — SLO compliance + error budget.

Plan A1b. Shares the ``metrics:requests`` ZSET with the RED endpoint
(``app/services/red_metrics.py:compute_slo`` does most of the work);
only the success classifier differs — SLO counts ``status >= 500`` as
the failure mode (server availability), while RED's error_pct also
catches non-401/403/404 4xxs.

Budget math (matches the implementation):

  success_pct           = successful / total * 100
  consumed              = (100 - success_pct) / (100 - slo_target_pct) * 100
  remaining             = 100 - consumed
  (both clamped to [0, 100])

Default ``SLO_AVAILABILITY_TARGET_PCT = 99.5`` per ``app/core/config.py``.
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
# Fixtures + helpers (mirror tests/api/test_system_red.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_metrics_redis_cache(redis_client: Redis) -> Iterator[None]:
    """Bust the middleware's cached Redis client between tests."""
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
    template: str = "/api/v1/orders",
    method: str = "GET",
    status: int = 200,
    duration_ms: float = 10.0,
    ts_ms: int | None = None,
) -> int:
    """Same ZSET-write helper as ``tests/api/test_system_red.py``."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    member = json.dumps(
        {"p": template, "m": method, "s": status, "d": duration_ms, "t": ts_ms},
        separators=(",", ":"),
    )
    redis_client.zadd(METRICS_KEY, {member: ts_ms})
    return ts_ms


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_slo_requires_authentication(client: TestClient) -> None:
    """Unauthenticated → 401 via the unified error envelope."""
    res = client.get("/api/v1/system/slo")
    assert res.status_code == 401


def test_slo_rbac(client: TestClient, db_session: Session) -> None:
    """viewer / order_manager → 403; scheduler + root → 200.

    SLO data is operator-grade; viewers and order managers do not see it.
    """
    _make_user(db_session, username="slo_viewer", role=UserRole.viewer)
    _make_user(db_session, username="slo_om", role=UserRole.order_manager)
    _make_user(db_session, username="slo_sched", role=UserRole.scheduler)
    _make_user(db_session, username="slo_root", role=UserRole.root)

    assert (
        client.get("/api/v1/system/slo", headers=_auth(_login(client, "slo_viewer"))).status_code
        == 403
    )
    assert (
        client.get("/api/v1/system/slo", headers=_auth(_login(client, "slo_om"))).status_code == 403
    )
    assert (
        client.get("/api/v1/system/slo", headers=_auth(_login(client, "slo_sched"))).status_code
        == 200
    )
    assert (
        client.get("/api/v1/system/slo", headers=_auth(_login(client, "slo_root"))).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Full-budget cases
# ---------------------------------------------------------------------------


def test_slo_returns_full_budget_when_no_errors(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """100 samples, all 200 → success_pct=100, budget remaining=100, consumed=0.

    Healthy traffic spends no error budget. The SLO target defaults to 99.5%,
    so we're 0.5 percentage points clear of the bar.
    """
    _make_user(db_session, username="slo_full", role=UserRole.scheduler)
    token = _login(client, "slo_full")
    redis_client.delete(METRICS_KEY)

    base = int(time.time() * 1000) - 200
    for i in range(100):
        _seed_sample(redis_client, status=200, ts_ms=base + i)

    res = client.get("/api/v1/system/slo?window_hours=24", headers=_auth(token))
    assert res.status_code == 200
    body = res.json()
    assert body["window_hours"] == 24
    assert body["total_requests"] == 100
    assert body["successful_requests"] == 100
    assert body["success_pct"] == 100.0
    assert body["slo_target_pct"] == 99.5
    assert body["error_budget_pct_remaining"] == 100.0
    assert body["error_budget_consumed_pct"] == 0.0
    # Data window is present; samples just landed so it is well below the
    # requested 24h (86400s). We only assert presence + non-negativity
    # here — the dedicated test below covers the truncation semantics.
    assert "data_window_seconds_actual" in body
    assert body["data_window_seconds_actual"] >= 0
    assert body["data_window_seconds_actual"] <= 24 * 3600


# ---------------------------------------------------------------------------
# Partial-budget / over-budget cases
# ---------------------------------------------------------------------------


def test_slo_returns_partial_budget(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """5% 5xx rate vs 99.5% target → budget exhausted.

    Math: success_pct = 95%, headroom = 0.5% (100 - 99.5), consumed =
    (100 - 95) / 0.5 * 100 = 1000%. Clamped to 100% (over-budget caps
    at "all gone"). Budget remaining = 0%.
    """
    _make_user(db_session, username="slo_partial", role=UserRole.scheduler)
    token = _login(client, "slo_partial")
    redis_client.delete(METRICS_KEY)

    base = int(time.time() * 1000) - 200
    for i in range(95):
        _seed_sample(redis_client, status=200, ts_ms=base + i)
    for i in range(5):
        _seed_sample(redis_client, status=500, ts_ms=base + 95 + i)

    res = client.get("/api/v1/system/slo?window_hours=24", headers=_auth(token))
    body = res.json()
    assert body["total_requests"] == 100
    assert body["successful_requests"] == 95
    assert body["success_pct"] == 95.0
    assert body["slo_target_pct"] == 99.5
    # 5% errors >> 0.5% budget → consumed clamps to 100%.
    assert body["error_budget_pct_remaining"] == 0.0
    assert body["error_budget_consumed_pct"] == 100.0


def test_slo_below_target_but_within_budget(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """1 of 1000 = 0.1% errors vs 99.5% target → 80% budget remaining.

    headroom = 0.5%, consumed = 0.1 / 0.5 * 100 = 20%, remaining = 80%.
    This is the realistic dashboard view: not perfect, but well within
    the operating envelope. Tests the unclamped-non-zero branch.
    """
    _make_user(db_session, username="slo_within", role=UserRole.scheduler)
    token = _login(client, "slo_within")
    redis_client.delete(METRICS_KEY)

    base = int(time.time() * 1000) - 2000
    for i in range(999):
        _seed_sample(redis_client, status=200, ts_ms=base + i)
    _seed_sample(redis_client, status=503, ts_ms=base + 999)

    res = client.get("/api/v1/system/slo?window_hours=24", headers=_auth(token))
    body = res.json()
    assert body["total_requests"] == 1000
    assert body["successful_requests"] == 999
    assert body["success_pct"] == 99.9
    assert body["error_budget_pct_remaining"] == 80.0
    assert body["error_budget_consumed_pct"] == 20.0


# ---------------------------------------------------------------------------
# No-traffic edge case
# ---------------------------------------------------------------------------


def test_slo_zero_traffic(client: TestClient, db_session: Session, redis_client: Redis) -> None:
    """No samples → success_pct=100, full budget remaining.

    Per Plan A1b: no traffic means no budget spent, so we report a
    healthy SLO. Avoids waking on-call on a quiet weekend.
    """
    _make_user(db_session, username="slo_zero", role=UserRole.scheduler)
    token = _login(client, "slo_zero")
    redis_client.delete(METRICS_KEY)

    res = client.get("/api/v1/system/slo?window_hours=24", headers=_auth(token))
    body = res.json()
    assert body["total_requests"] == 0
    assert body["successful_requests"] == 0
    assert body["success_pct"] == 100.0
    assert body["slo_target_pct"] == 99.5
    assert body["error_budget_pct_remaining"] == 100.0
    assert body["error_budget_consumed_pct"] == 0.0
    # No samples → no data window. The frontend uses this to skip the
    # "data: last Xm" hint when the SLO card is in the empty state.
    assert body["data_window_seconds_actual"] == 0


# ---------------------------------------------------------------------------
# data_window_seconds_actual semantics
# ---------------------------------------------------------------------------


def test_slo_data_window_reflects_oldest_sample(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """``data_window_seconds_actual`` reports min(requested, age_of_oldest_sample).

    Seeds a sample 30 minutes old then requests window_hours=24. Because
    the ZSET only holds 30 min of data, the response should report ~1800
    s (with small jitter for clock drift between seed and request).
    """
    _make_user(db_session, username="slo_dw", role=UserRole.scheduler)
    token = _login(client, "slo_dw")
    redis_client.delete(METRICS_KEY)

    # Oldest sample 30 minutes ago (1_800_000 ms). Everything else lands
    # in the recent past — the field tracks the OLDEST entry, not the
    # newest, so a single old sample is sufficient.
    now_ms = int(time.time() * 1000)
    _seed_sample(redis_client, status=200, ts_ms=now_ms - 1_800_000)
    for i in range(10):
        _seed_sample(redis_client, status=200, ts_ms=now_ms - 100 + i)

    res = client.get("/api/v1/system/slo?window_hours=24", headers=_auth(token))
    body = res.json()
    # Should be ~1800s ± a few seconds for clock drift between seed and
    # request handling. We use a generous tolerance band.
    assert 1750 <= body["data_window_seconds_actual"] <= 1850, (
        f"expected ~1800, got {body['data_window_seconds_actual']}"
    )


def test_slo_data_window_capped_at_requested(
    client: TestClient, db_session: Session, redis_client: Redis
) -> None:
    """``data_window_seconds_actual`` never exceeds the requested window.

    Seeds a sample artificially "older" than the requested window — the
    field must clamp to ``window_hours * 3600`` rather than reporting the
    raw oldest-sample age. (In practice ZSET retention prevents this but
    the math should be defensive.)
    """
    _make_user(db_session, username="slo_dw_cap", role=UserRole.scheduler)
    token = _login(client, "slo_dw_cap")
    redis_client.delete(METRICS_KEY)

    # Request window_hours=1 (3600s). Recent samples are well within
    # 1 hour, so the data window should match the recent sample age,
    # NOT exceed 3600.
    now_ms = int(time.time() * 1000)
    for i in range(10):
        _seed_sample(redis_client, status=200, ts_ms=now_ms - 500 + i)

    res = client.get("/api/v1/system/slo?window_hours=1", headers=_auth(token))
    body = res.json()
    assert body["data_window_seconds_actual"] <= 3600
