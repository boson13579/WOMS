"""Tests for Task 3: scheduling lock, hard pin, and soft pin mechanisms.

TDD: all tests in this file are written BEFORE implementation (RED phase).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import date
from unittest.mock import AsyncMock

import bcrypt
import fakeredis.aioredis
import pytest
from app.core.redis import get_redis
from app.core.scheduling_lock import (
    SCHEDULING_LOCK_KEY,
    SCHEDULING_LOCK_TTL_SECONDS,
    acquire_scheduling_lock,
    is_scheduling_locked,
    release_scheduling_lock,
    scheduling_lock_context,
)
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_orders.py)
# ---------------------------------------------------------------------------

_BASE = "/api/v1/orders"


def _make_user(
    db: Session,
    *,
    username: str,
    password: str = "Passw0rd!",
    role: UserRole = UserRole.scheduler,
) -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client: TestClient, username: str, password: str = "Passw0rd!") -> str:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _make_order(
    db: Session,
    *,
    created_by: uuid.UUID,
    customer_name: str = "Lock Test Corp",
    wafer_quantity: int = 100,
    requested_delivery_date: date = date(2026, 9, 1),
    status: OrderStatus = OrderStatus.pending,
) -> Order:
    order = Order(
        order_number=f"ORD-LOCK-{uuid.uuid4().hex[:6].upper()}",
        customer_name=customer_name,
        wafer_quantity=wafer_quantity,
        requested_delivery_date=requested_delivery_date,
        status=status,
        created_by=created_by,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis_instance() -> fakeredis.aioredis.FakeRedis:
    """A fresh, unlocked FakeRedis for each test."""
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def locked_client(
    db_session: Session,
    fake_redis_instance: fakeredis.aioredis.FakeRedis,
) -> Generator[TestClient, None, None]:
    """TestClient with a pre-locked scheduling Redis."""
    from app.core.db import get_db
    from app.main import app

    asyncio.run(fake_redis_instance.set(SCHEDULING_LOCK_KEY, "1", ex=SCHEDULING_LOCK_TTL_SECONDS))

    async def _override_get_redis() -> AsyncGenerator[Redis, None]:
        yield fake_redis_instance  # type: ignore[misc]

    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = _override_get_redis
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Hard Pin tests
# ---------------------------------------------------------------------------


def test_lock_order_success(client: TestClient, db_session: Session) -> None:
    """POST /{order_id}/lock by scheduler → 200, is_locked=True."""
    user = _make_user(db_session, username="lk_success_sched")
    token = _login(client, "lk_success_sched")
    order = _make_order(db_session, created_by=user.id)

    resp = client.post(f"{_BASE}/{order.id}/lock", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_locked"] is True
    assert data["locked_by"] is not None
    assert data["locked_at"] is not None


def test_lock_order_idempotent(client: TestClient, db_session: Session) -> None:
    """POST lock on already-locked order returns 200 (no error)."""
    user = _make_user(db_session, username="lk_idemp_sched")
    token = _login(client, "lk_idemp_sched")
    order = _make_order(db_session, created_by=user.id)

    client.post(f"{_BASE}/{order.id}/lock", headers=_auth(token))
    resp = client.post(f"{_BASE}/{order.id}/lock", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["is_locked"] is True


def test_unlock_order_success(client: TestClient, db_session: Session) -> None:
    """DELETE /{order_id}/lock by scheduler → 200, is_locked=False."""
    user = _make_user(db_session, username="unlk_sched")
    token = _login(client, "unlk_sched")
    order = _make_order(db_session, created_by=user.id)

    client.post(f"{_BASE}/{order.id}/lock", headers=_auth(token))
    resp = client.delete(f"{_BASE}/{order.id}/lock", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_locked"] is False
    assert data["locked_by"] is None
    assert data["locked_at"] is None


def test_unlock_order_idempotent(client: TestClient, db_session: Session) -> None:
    """DELETE lock on already-unlocked order returns 200 (no error)."""
    user = _make_user(db_session, username="unlk_idemp_sched")
    token = _login(client, "unlk_idemp_sched")
    order = _make_order(db_session, created_by=user.id)

    resp = client.delete(f"{_BASE}/{order.id}/lock", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["is_locked"] is False


def test_lock_order_by_viewer_returns_403(client: TestClient, db_session: Session) -> None:
    """Viewer role cannot lock an order → 403."""
    sched = _make_user(db_session, username="lk403_sched")
    _make_user(db_session, username="lk403_viewer", role=UserRole.viewer)
    token = _login(client, "lk403_viewer")
    order = _make_order(db_session, created_by=sched.id)

    resp = client.post(f"{_BASE}/{order.id}/lock", headers=_auth(token))
    assert resp.status_code == 403


def test_lock_order_not_found_returns_404(client: TestClient, db_session: Session) -> None:
    """POST lock for non-existent order → 404."""
    _make_user(db_session, username="lk404_sched")
    token = _login(client, "lk404_sched")
    fake_id = str(uuid.uuid4())

    resp = client.post(f"{_BASE}/{fake_id}/lock", headers=_auth(token))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Soft Pin tests
# ---------------------------------------------------------------------------


def test_soft_pin_set_success(client: TestClient, db_session: Session) -> None:
    """PATCH /{order_id}/soft-pin → 200, soft_pin_date updated."""
    user = _make_user(db_session, username="sp_set_sched")
    token = _login(client, "sp_set_sched")
    order = _make_order(db_session, created_by=user.id)

    resp = client.patch(
        f"{_BASE}/{order.id}/soft-pin",
        json={"preferred_date": "2026-09-10"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["soft_pin_date"] == "2026-09-10"


def test_soft_pin_clear_success(client: TestClient, db_session: Session) -> None:
    """DELETE /{order_id}/soft-pin → 200, soft_pin_date=None."""
    user = _make_user(db_session, username="sp_clr_sched")
    token = _login(client, "sp_clr_sched")
    order = _make_order(db_session, created_by=user.id)

    client.patch(
        f"{_BASE}/{order.id}/soft-pin",
        json={"preferred_date": "2026-09-10"},
        headers=_auth(token),
    )
    resp = client.delete(f"{_BASE}/{order.id}/soft-pin", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["soft_pin_date"] is None


def test_soft_pin_by_order_manager_returns_403(client: TestClient, db_session: Session) -> None:
    """order_manager role cannot set soft pin → 403."""
    sched = _make_user(db_session, username="sp403_sched")
    _make_user(db_session, username="sp403_mgr", role=UserRole.order_manager)
    mgr_token = _login(client, "sp403_mgr")
    order = _make_order(db_session, created_by=sched.id)

    resp = client.patch(
        f"{_BASE}/{order.id}/soft-pin",
        json={"preferred_date": "2026-09-10"},
        headers=_auth(mgr_token),
    )
    assert resp.status_code == 403


def test_soft_pin_invalid_date_returns_422(client: TestClient, db_session: Session) -> None:
    """Non-date string for preferred_date → 422 validation error."""
    user = _make_user(db_session, username="sp422_sched")
    token = _login(client, "sp422_sched")
    order = _make_order(db_session, created_by=user.id)

    resp = client.patch(
        f"{_BASE}/{order.id}/soft-pin",
        json={"preferred_date": "not-a-date"},
        headers=_auth(token),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Scheduling Lock — router integration tests
# ---------------------------------------------------------------------------


def test_update_order_during_scheduling_returns_423(
    locked_client: TestClient, db_session: Session
) -> None:
    """PATCH /orders/{id} while scheduling lock is held → 423."""
    user = _make_user(db_session, username="sl_upd_sched")
    token = _login(locked_client, "sl_upd_sched")
    order = _make_order(db_session, created_by=user.id)

    resp = locked_client.patch(
        f"{_BASE}/{order.id}",
        json={"wafer_quantity": 200, "version_id": order.version_id},
        headers=_auth(token),
    )
    assert resp.status_code == 423
    body = resp.json()
    assert body["error"]["code"] == 423


def test_delete_order_during_scheduling_returns_423(
    locked_client: TestClient, db_session: Session
) -> None:
    """DELETE /orders/{id} while scheduling lock is held → 423."""
    user = _make_user(db_session, username="sl_del_sched")
    token = _login(locked_client, "sl_del_sched")
    order = _make_order(db_session, created_by=user.id)

    resp = locked_client.delete(f"{_BASE}/{order.id}", headers=_auth(token))
    assert resp.status_code == 423
    assert resp.json()["error"]["code"] == 423


def test_lock_order_during_scheduling_returns_423(
    locked_client: TestClient, db_session: Session
) -> None:
    """POST /orders/{id}/lock while scheduling lock is held → 423."""
    user = _make_user(db_session, username="sl_lk_sched")
    token = _login(locked_client, "sl_lk_sched")
    order = _make_order(db_session, created_by=user.id)

    resp = locked_client.post(f"{_BASE}/{order.id}/lock", headers=_auth(token))
    assert resp.status_code == 423
    assert resp.json()["error"]["code"] == 423


# ---------------------------------------------------------------------------
# Scheduling Lock — unit tests (call scheduling_lock.py functions directly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_lock_succeeds_when_free() -> None:
    """acquire_scheduling_lock returns a token string when no lock is held."""
    redis = fakeredis.aioredis.FakeRedis()
    result = await acquire_scheduling_lock(redis)
    assert result is not None


@pytest.mark.asyncio
async def test_acquire_lock_fails_when_held() -> None:
    """acquire_scheduling_lock returns None when lock already exists."""
    redis = fakeredis.aioredis.FakeRedis()
    await acquire_scheduling_lock(redis)
    result = await acquire_scheduling_lock(redis)
    assert result is None


@pytest.mark.asyncio
async def test_release_lock_succeeds() -> None:
    """release_scheduling_lock deletes the key; is_scheduling_locked returns False."""
    fake = fakeredis.aioredis.FakeRedis()
    token = await acquire_scheduling_lock(fake)

    async def _eval(script: str, numkeys: int, key: str, tok: str) -> int:
        stored = await fake.get(key)
        if stored is not None and stored.decode() == tok:
            await fake.delete(key)
            return 1
        return 0

    fake.eval = AsyncMock(side_effect=_eval)  # type: ignore[method-assign]
    assert token is not None
    await release_scheduling_lock(fake, token)
    locked = await is_scheduling_locked(fake)
    assert locked is False


@pytest.mark.asyncio
async def test_release_lock_wrong_token() -> None:
    """release_scheduling_lock returns False and leaves the key when token does not match."""
    fake = fakeredis.aioredis.FakeRedis()
    await acquire_scheduling_lock(fake)
    fake.eval = AsyncMock(return_value=0)  # type: ignore[method-assign]
    released = await release_scheduling_lock(fake, str(uuid.uuid4()))
    assert released is False
    assert await is_scheduling_locked(fake) is True


@pytest.mark.asyncio
async def test_lock_expires_after_ttl() -> None:
    """Lock key has TTL <= SCHEDULING_LOCK_TTL_SECONDS after acquire."""
    redis = fakeredis.aioredis.FakeRedis()
    await acquire_scheduling_lock(redis)
    ttl = await redis.ttl(SCHEDULING_LOCK_KEY)
    assert 0 < ttl <= SCHEDULING_LOCK_TTL_SECONDS


@pytest.mark.asyncio
async def test_scheduling_lock_context_acquires_and_releases() -> None:
    """scheduling_lock_context holds the lock inside the block and releases on exit."""
    fake = fakeredis.aioredis.FakeRedis()

    async def _eval(script: str, numkeys: int, key: str, tok: str) -> int:
        stored = await fake.get(key)
        if stored is not None and stored.decode() == tok:
            await fake.delete(key)
            return 1
        return 0

    fake.eval = AsyncMock(side_effect=_eval)  # type: ignore[method-assign]

    async with scheduling_lock_context(fake):
        assert await is_scheduling_locked(fake) is True

    assert await is_scheduling_locked(fake) is False


@pytest.mark.asyncio
async def test_scheduling_lock_context_raises_when_already_locked() -> None:
    """scheduling_lock_context raises RuntimeError if lock is already held."""
    redis = fakeredis.aioredis.FakeRedis()
    await redis.set(SCHEDULING_LOCK_KEY, "1", ex=SCHEDULING_LOCK_TTL_SECONDS)

    with pytest.raises(RuntimeError):
        async with scheduling_lock_context(redis):
            pass
