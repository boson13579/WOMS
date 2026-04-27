"""Database infrastructure — engine, session factory, and FastAPI dependency.

This module is *infrastructure-only*: it knows how to connect to PostgreSQL and
how to hand out a Session-per-request. It does NOT define the ORM Base or any
entity — those live in `app.models` (the entity layer).

Layered architecture rule: `core/db.py` may be imported by anyone, but
`core/db.py` itself only depends on `core/config.py`.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _build_engine() -> Engine:
    """Build the SQLAlchemy engine using validated settings.

    `pool_pre_ping=True` guards against stale connections when Postgres or a
    network proxy silently drops idle connections — critical in containerized
    deployments where the DB and the app may be restarted independently.
    """
    settings = get_settings()
    return create_engine(
        settings.database_url_str,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
        future=True,  # SQLAlchemy 2.0 style
    )


# Module-level singletons — created lazily on first import.
engine: Engine = _build_engine()

# `expire_on_commit=False` keeps attributes accessible after commit without
# forcing a refetch; combined with our request-scoped session, this is safe.
SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a transactional Session per request.

    Usage in a router:

        @router.get("/orders/{order_id}")
        def read_order(order_id: UUID, db: Session = Depends(get_db)) -> ...:
            ...

    The session is closed automatically when the request finishes, even on
    exceptions — `try/finally` guarantees cleanup so the connection returns
    to the pool.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
