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


def get_pool_stats() -> dict[str, int | float] | None:
    """Snapshot the SQLAlchemy connection pool for the resources endpoint.

    Returns ``None`` if the engine's pool doesn't expose the standard
    ``QueuePool`` interface (e.g. ``NullPool`` in some test configurations
    where ``.size()`` would raise). The dashboard treats ``None`` as
    "hide this section" rather than a hard error.

    ``utilization_pct = checked_out / (size + max_overflow) * 100`` —
    saturation here means new requests have to wait on a connection,
    which is exactly the operator-facing signal we want to surface.
    """
    settings = get_settings()
    max_overflow = settings.DB_MAX_OVERFLOW
    try:
        pool = engine.pool
        size = pool.size()  # type: ignore[attr-defined]
        checked_out = pool.checkedout()  # type: ignore[attr-defined]
        overflow = pool.overflow()  # type: ignore[attr-defined]
    except AttributeError:
        # NullPool (and SingletonThreadPool) don't expose .size() / .overflow().
        # Returning None here keeps the resources endpoint honest: "we don't
        # have pool stats for this pool type" rather than fabricating zeros.
        return None
    capacity = max(size + max_overflow, 1)
    utilization_pct = round(checked_out / capacity * 100, 1)
    return {
        "size": int(size),
        "checked_out": int(checked_out),
        "overflow": int(overflow),
        "max_overflow": int(max_overflow),
        "utilization_pct": utilization_pct,
    }
