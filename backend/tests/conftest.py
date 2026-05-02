"""Pytest fixtures shared across the backend test suite.

This conftest is the *foundation* for all DB-touching tests. It uses
Testcontainers to spin up an ephemeral PostgreSQL 15 container for the entire
test session, applies the live SQLAlchemy schema (via `Base.metadata`), and
hands each test a transactional `Session` that rolls back on teardown.

Why Testcontainers (not SQLite)?
    Per docs/RULES.md §5 we must mirror production. SQLite silently accepts JSONB
    columns, ignores Postgres-only DDL, and lacks server-default semantics —
    a green SQLite test is a false positive. Real Postgres, real bugs.

Performance notes:
    * The container is `scope="session"` so it boots once per `pytest` run.
    * Each test gets a SAVEPOINT-style nested transaction that rolls back,
      so tests are isolated without paying the cost of recreating the schema.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator, Iterator

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

# --- 1. Postgres container (session-wide) ------------------------------------


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """Boot a real PostgreSQL 15 container for the full test session.

    The container is reused across all tests for performance; isolation is
    provided by the per-test transaction rollback in `db_session` below.
    """
    with PostgresContainer("postgres:15-alpine") as pg:
        # Some downstream code calls `get_settings()` which requires DATABASE_URL
        # in the environment. Inject the container URL before any test imports
        # `app.core.config`.
        os.environ["DATABASE_URL"] = pg.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql+psycopg"
        )
        os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
        os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod")
        yield pg


# --- 2. Engine + schema (session-wide) ---------------------------------------


@pytest.fixture(scope="session")
def engine(postgres_container: PostgresContainer) -> Iterator[Engine]:
    """SQLAlchemy engine bound to the test container.

    Creates all tables once via `Base.metadata.create_all` — bypassing Alembic
    here is intentional and standard practice: Alembic is verified separately
    by a dedicated migration test.
    """
    # Imported lazily so env vars set in `postgres_container` are honored.
    from app.models.base_class import Base

    url = os.environ["DATABASE_URL"]
    eng = create_engine(url, future=True)
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(bind=eng)
        eng.dispose()


# --- 3. Per-test transactional session ---------------------------------------


@pytest.fixture
def db_session(engine: Engine) -> Generator[Session, None, None]:
    """Yield a Session wrapped in a SAVEPOINT-style nested transaction.

    The outer transaction is rolled back on teardown so each test starts with
    a clean slate without the cost of dropping/recreating tables.

    Pattern adapted from SQLAlchemy's "Joining a Session into an External
    Transaction" recipe.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session_factory = sessionmaker(bind=connection, expire_on_commit=False)
    session = session_factory()

    # Restart a SAVEPOINT every time the application code calls `commit()`,
    # so commits inside the test don't escape our outer rollback.
    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(_session: Session, trans: object) -> None:
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# --- 4. FakeRedis (per-test, unlocked) ---------------------------------------


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    """A fresh, unlocked FakeRedis instance for each test."""
    return fakeredis.aioredis.FakeRedis()


# --- 5. FastAPI TestClient with `get_db` and `get_redis` overridden ----------


@pytest.fixture
def client(db_session: Session, fake_redis: fakeredis.aioredis.FakeRedis) -> Iterator[TestClient]:
    """A `TestClient` whose `get_db` and `get_redis` dependencies are overridden.

    `get_db` yields the rolled-back session so DB state is isolated per test.
    `get_redis` yields an empty FakeRedis so no real Redis is required.
    """
    from app.core.db import get_db
    from app.core.redis import get_redis
    from app.main import app

    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    async def _override_get_redis() -> AsyncGenerator[Redis, None]:
        yield fake_redis  # type: ignore[misc]

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = _override_get_redis
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
