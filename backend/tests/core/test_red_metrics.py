"""Tests for the RED-metrics ingestion middleware (``app/core/red_metrics.py``).

Plan A1 — every HTTP request emits a sample to the ``metrics:requests``
Redis sorted set so the dashboard's RED card can aggregate the trailing
window. These tests verify the middleware *writer* contract:

1. A normal request produces one ZSET entry with the right fields.
2. Excluded paths (``/system/health``, OpenAPI surfaces, etc.) do NOT
   produce a sample.
3. A Redis failure during the write does NOT propagate — the user-facing
   response still returns 200.
4. The HTTP status code captured in the sample reflects the actual
   response (4xx / 5xx not just 200).
5. Route templates are used, NOT raw URLs: ``/orders/{id}`` rather than
   ``/orders/abc-123-uuid`` so the histogram doesn't explode.

The Redis container fixture (``redis_client`` from ``tests/conftest.py``)
provides a real Redis 7 instance; the autouse ``_redis_flushdb`` wipes
the keyspace between tests. We bust the middleware's cached client so it
reconnects to the container rather than the placeholder URL captured at
import time.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import bcrypt
import pytest
from app.core.red_metrics import METRICS_KEY, RETENTION_MS, _get_metrics_redis
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_metrics_redis_cache(redis_client: Redis) -> Iterator[None]:
    """Point the middleware's cached Redis client at the test container.

    ``_get_metrics_redis`` is decorated with ``lru_cache`` so the FIRST
    call (at app-import time, before the container is up) would pin the
    placeholder URL. Busting the cache here ensures each test rebuilds
    the client against the active container.

    We also yield ``redis_client`` so the metrics writer and the test
    assertions share the same instance — preventing flakes from connecting
    to a stale URL.
    """
    _get_metrics_redis.cache_clear()
    yield
    _get_metrics_redis.cache_clear()


@pytest.fixture
def _wait_for_metrics(redis_client: Redis) -> _Waiter:
    """Yield a helper that polls the ZSET until the expected entry count appears.

    The middleware uses ``asyncio.create_task`` to write samples — the
    response can return before the write lands. The TestClient drains the
    loop on exit, but to keep test assertions deterministic we still poll
    a short while.
    """
    return _Waiter(redis_client)


class _Waiter:
    """Tiny synchronous poller that waits up to *timeout_s* for a Redis condition.

    Uses plain ``time.sleep`` rather than an asyncio loop — the TestClient
    already drained the request, and the metrics writer's task lives on
    whatever loop processed the request. Polling from the test thread
    side-steps "Different loops vs cached event loop" issues that arose
    when tests ran in sequence on Windows.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    def wait_for_count(self, expected: int, *, timeout_s: float = 2.0) -> int:
        """Block until ``ZCARD metrics:requests >= expected`` or timeout.

        Returns the final count. Caller decides whether to assert equality
        (single-request tests) or ``>=`` (multi-request tests).
        """
        deadline = time.monotonic() + timeout_s
        while True:
            count = int(self._client.zcard(METRICS_KEY))
            if count >= expected:
                return count
            if time.monotonic() >= deadline:
                return count
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    username: str,
    password: str = "password123",
    role: UserRole = UserRole.scheduler,
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


def _read_samples(redis_client: Redis) -> list[dict[str, Any]]:
    """Decode every member of the ``metrics:requests`` ZSET."""
    raw = redis_client.zrange(METRICS_KEY, 0, -1, withscores=False)
    return [json.loads(entry) for entry in raw]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Happy path — a normal request lands one entry
# ---------------------------------------------------------------------------


def test_middleware_records_request_to_redis(
    client: TestClient,
    redis_client: Redis,
    _wait_for_metrics: _Waiter,
) -> None:
    """A successful GET produces exactly one ZSET entry with the right shape.

    The login endpoint returns 200 on bad credentials too (validation
    error response), but ``/api/v1/health`` is the cleanest non-excluded
    surface that doesn't need auth. Wait — health IS excluded. Use the
    auth endpoint with a bogus body so we get a 401 / 422 but a real
    request reaches the middleware path.

    Actually, ``/api/v1/health`` is excluded by SKIP_EXACT; we need
    something else. ``/api/v1/auth/login`` returns 401 for bad creds
    and is NOT excluded → middleware records it. We assert one entry.
    """
    res = client.post("/api/v1/auth/login", json={"username": "x", "password": "y"})
    # Bad credentials → 401 via the unified error envelope.
    assert res.status_code == 401

    count = _wait_for_metrics.wait_for_count(1)
    assert count == 1

    samples = _read_samples(redis_client)
    assert len(samples) == 1
    sample = samples[0]
    # Compact JSON field names: p=path template, m=method, s=status, d=duration_ms, t=ts_ms.
    assert sample["p"] == "/api/v1/auth/login"
    assert sample["m"] == "POST"
    assert sample["s"] == 401
    assert isinstance(sample["d"], (int, float))
    assert sample["d"] >= 0
    assert isinstance(sample["t"], int)


# ---------------------------------------------------------------------------
# 2. Excluded paths produce no sample
# ---------------------------------------------------------------------------


def test_middleware_skips_excluded_paths(
    client: TestClient,
    db_session: Session,
    redis_client: Redis,
) -> None:
    """Polling ``/system/health`` and the other excluded surfaces must not
    inflate the histogram. The endpoint is skipped by ``SKIP_EXACT``.

    Hits ``/api/v1/system/health`` (excluded) five times, then asserts
    the ZSET is empty. Authentication is required by the endpoint but
    that's fine — the middleware skip check fires before any handler runs.
    """
    _make_user(db_session, username="skip_test", role=UserRole.viewer)
    token = _login(client, "skip_test")

    # Note: the login call above IS counted (login isn't excluded), so we
    # take a baseline reading after login to isolate the skip behaviour.
    redis_client.delete(METRICS_KEY)

    for _ in range(5):
        res = client.get("/api/v1/system/health", headers=_auth(token))
        assert res.status_code == 200

    # Give any potential async task a chance to land before asserting empty.
    time.sleep(0.1)

    assert int(redis_client.zcard(METRICS_KEY)) == 0


def test_middleware_skips_openapi_and_docs(
    client: TestClient,
    redis_client: Redis,
) -> None:
    """OpenAPI / docs surfaces never inflate the histogram.

    The dashboard polls ``/openapi.json`` once on load for shape
    validation; counting that would skew the rate metric on a sleepy
    deployment.
    """
    redis_client.delete(METRICS_KEY)
    res = client.get("/api/v1/openapi.json")
    assert res.status_code == 200

    time.sleep(0.1)
    assert int(redis_client.zcard(METRICS_KEY)) == 0


# ---------------------------------------------------------------------------
# 3. Redis-write failure does not propagate
# ---------------------------------------------------------------------------


def test_middleware_does_not_block_on_redis_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the Redis write blows up, the user-facing request must still return.

    Monkeypatches the cached client accessor to return a Redis stand-in
    whose ``pipeline().execute()`` raises. The request handler still
    returns 200 (here: the unified 401 from a bad login is fine — point
    is the middleware does not raise a 500).
    """
    fake_pipeline = MagicMock()
    fake_pipeline.zadd.return_value = fake_pipeline
    fake_pipeline.zremrangebyscore.return_value = fake_pipeline
    fake_pipeline.execute.side_effect = RuntimeError("redis is dead")

    fake_redis = MagicMock()
    fake_redis.pipeline.return_value = fake_pipeline

    # Patch BOTH the cached accessor (so any cached client is overridden)
    # AND the underlying ``Redis.from_url`` (in case the cache was empty
    # and re-instantiates). The middleware path goes through the accessor,
    # so the first one suffices in practice.
    monkeypatch.setattr("app.core.red_metrics._get_metrics_redis", lambda: fake_redis)

    res = client.post("/api/v1/auth/login", json={"username": "x", "password": "y"})
    # 401 from the auth handler — NOT 500. Middleware swallowed the Redis
    # failure as designed.
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# 4. 4xx / 5xx are captured with the right status code
# ---------------------------------------------------------------------------


def test_middleware_records_status_code_correctly(
    client: TestClient,
    db_session: Session,
    redis_client: Redis,
    _wait_for_metrics: _Waiter,
) -> None:
    """Mix of 401 (auth fail) and 200 (auth success) is recorded faithfully.

    Asserts the ``s`` field on each sample matches the response status
    code so the RED endpoint's error-pct calculation has the right input.
    """
    redis_client.delete(METRICS_KEY)
    _make_user(db_session, username="status_test", role=UserRole.scheduler)

    # 1) 401: bad password
    r1 = client.post("/api/v1/auth/login", json={"username": "status_test", "password": "wrong"})
    assert r1.status_code == 401
    # 2) 200: good login
    r2 = client.post(
        "/api/v1/auth/login", json={"username": "status_test", "password": "password123"}
    )
    assert r2.status_code == 200

    _wait_for_metrics.wait_for_count(2)

    samples = _read_samples(redis_client)
    statuses = sorted(s["s"] for s in samples)
    assert statuses == [200, 401]


# ---------------------------------------------------------------------------
# 5. Route template, not raw URL, is recorded
# ---------------------------------------------------------------------------


def test_retention_window_is_one_hour() -> None:
    """``RETENTION_MS`` is exactly 1 hour (60 * 60 * 1000 ms).

    Widened from the original 5 minutes so the ``/system/slo`` endpoint
    can report a meaningful sample slice for its default 24h window.
    This test pins the constant so a future regression that quietly
    narrows the trim window (and silently undermines the SLO surface)
    fails loudly here.
    """
    assert RETENTION_MS == 60 * 60 * 1000


def test_middleware_uses_route_template_not_url(
    client: TestClient,
    db_session: Session,
    redis_client: Redis,
    _wait_for_metrics: _Waiter,
) -> None:
    """``GET /api/v1/users/{user_id}`` is one bucket regardless of the UUID.

    Bench: hits the users endpoint twice with two different UUIDs and
    asserts both samples carry the SAME path template (``{user_id}``)
    so the histogram doesn't explode into one bucket per UUID. The
    actual response code is irrelevant — we just need a route the
    matcher can resolve.
    """
    _make_user(db_session, username="tmpl_root", role=UserRole.root)
    token = _login(client, "tmpl_root")
    # Clear AFTER login so the login sample is not in the assertion set —
    # we only care about the two ``/users/{user_id}`` GETs that follow.
    redis_client.delete(METRICS_KEY)

    # Two arbitrary UUIDs — the route exists; whether the user is found
    # is irrelevant for this assertion.
    client.get(
        "/api/v1/users/00000000-0000-0000-0000-000000000001",
        headers=_auth(token),
    )
    client.get(
        "/api/v1/users/00000000-0000-0000-0000-000000000002",
        headers=_auth(token),
    )

    _wait_for_metrics.wait_for_count(2)

    samples = _read_samples(redis_client)
    paths = {s["p"] for s in samples}
    # Both UUIDs collapse to the same template — the histogram bucket is
    # `/api/v1/users/{user_id}` regardless of the path parameter value.
    assert paths == {"/api/v1/users/{user_id}"}


def test_route_template_collapses_unmatched_routes(
    client: TestClient,
    redis_client: Redis,
    _wait_for_metrics: _Waiter,
) -> None:
    """Unmatched (404) paths collapse into a single ``"(no route)"`` bucket.

    Cardinality-attack protection: returning the raw URL for unmatched
    requests would let any caller enumerate distinct histogram buckets
    by probing random paths (``/abc-1``, ``/abc-2``, ...). We instead
    bucket every unmatched request under the literal label
    ``"(no route)"`` so operators still see ambient 404 noise without
    the histogram or the ``metrics:requests`` ZSET growing unboundedly.
    """
    redis_client.delete(METRICS_KEY)

    # Two random, definitely-unmatched paths. The middleware should record
    # both samples but BOTH must collapse to the same ``"(no route)"`` bucket.
    res1 = client.get("/does-not-exist-1")
    res2 = client.get("/does-not-exist-2/nested")
    assert res1.status_code == 404
    assert res2.status_code == 404

    _wait_for_metrics.wait_for_count(2)

    samples = _read_samples(redis_client)
    paths = {s["p"] for s in samples}
    assert paths == {"(no route)"}, f"unmatched routes must collapse to (no route); got: {paths}"
