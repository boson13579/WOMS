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
from collections.abc import Generator, Iterator

# ---------------------------------------------------------------------------
# Module-level env-var defaults
# ---------------------------------------------------------------------------
# ``app.core.config.Settings`` declares ``DATABASE_URL`` / ``REDIS_URL`` /
# ``JWT_SECRET`` as required fields. Any module that calls ``get_settings()``
# at import-time (e.g. ``app/services/scheduling.py:_settings = get_settings()``)
# would otherwise fail config validation during test collection — well before
# the session-scoped container fixtures get a chance to inject the real URLs.
#
# We seed safe placeholders here so collection always succeeds; the actual
# container fixtures below overwrite them with reachable URLs (and bust
# ``get_settings()``'s lru_cache so the override is picked up).
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://test:test@localhost/test",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod")


import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

# --- 1. Postgres container (session-wide) ------------------------------------


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """Boot a real PostgreSQL 15 container for the full test session.

    The container is reused across all tests for performance; isolation is
    provided by the per-test transaction rollback in `db_session` below.
    """
    with PostgresContainer("postgres:15-alpine") as pg:
        os.environ["DATABASE_URL"] = pg.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql+psycopg"
        )
        _bust_settings_cache()
        yield pg


# --- 1b. Redis container (session-wide) --------------------------------------


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    """Boot a real Redis 7 container for the full test session.

    Same rationale as ``postgres_container``: docs/RULES.md §5 mandates that
    integration tests mirror production. A hand-rolled ``_FakeRedis`` only
    covers the subset of commands we happened to use today, so adding a new
    Redis command silently passes tests until production blows up. A real
    container is the only honest stand-in.

    Isolation between tests is handled by the ``_redis_flushdb`` autouse
    fixture below — every test starts with an empty keyspace.
    """
    with RedisContainer("redis:7-alpine") as r:
        host = r.get_container_host_ip()
        port = r.get_exposed_port(6379)
        # Point the entire app at the container by overriding the env var
        # the ``Settings`` model reads, then busting ``get_settings()`` and
        # the per-module ``_redis()`` lru_caches so the new URL takes effect.
        os.environ["REDIS_URL"] = f"redis://{host}:{port}/0"
        _bust_settings_cache()
        yield r


@pytest.fixture(scope="session")
def redis_client(redis_container: RedisContainer) -> Iterator[Redis]:
    """Reusable Redis client bound to the test container.

    Tests should depend on this fixture (not call ``Redis.from_url`` themselves)
    so the connection pool is shared and FLUSHDB hits the right instance.
    """
    client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(autouse=True)
def _redis_flushdb(redis_client: Redis) -> Iterator[None]:
    """Wipe the Redis keyspace before each test.

    Replaces the old per-test ``_FakeRedis()`` instantiation pattern — one
    process-wide container with a clean keyspace per test gives the same
    isolation guarantee at a fraction of the boilerplate. autouse so every
    test gets the wipe without opting in.
    """
    redis_client.flushdb()
    yield


def _bust_settings_cache() -> None:
    """Invalidate every lru_cached settings / redis-client accessor.

    Called whenever a fixture mutates one of the env vars (``DATABASE_URL``
    / ``REDIS_URL``) so the next ``get_settings()`` / ``_redis()`` call
    re-reads the updated environment. Without this, the first import of
    ``app.core.config`` would freeze the placeholder URL in the cache and
    no amount of os.environ surgery would change it for subsequent code.
    """
    from app.api.v1 import schedule as api_schedule
    from app.core.config import get_settings
    from app.services import schedule_queue
    from app.workers import scheduling as worker_scheduling

    get_settings.cache_clear()
    api_schedule._redis.cache_clear()
    schedule_queue._redis.cache_clear()
    worker_scheduling._get_redis.cache_clear()


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


# --- 4. FastAPI TestClient with `get_db` overridden --------------------------


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """A `TestClient` whose `get_db` dependency yields the rolled-back session.

    This means request handlers run against the same isolated transaction the
    test inspects directly — no flaky cross-fixture state.
    """
    from app.core.db import get_db
    from app.main import app

    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
