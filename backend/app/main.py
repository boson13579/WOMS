"""FastAPI application factory.

This module is the *only* place where the HTTP layer, error handlers, logging,
and middleware are wired together. Keeping the wiring centralized makes the
boot sequence auditable and unit-testable.

Run with:  `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import ConnectionPool

from app.api.errors import register_exception_handlers
from app.api.v1 import api_router as api_v1_router
from app.api.v1.websocket import event_consumer_loop
from app.core.config import get_settings
from app.core.logger import configure_logging, correlation_id_middleware

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks.

    Boots the WebSocket Redis-bridge consumer as a background task so events
    published by the Celery worker reach this process's connected sockets.
    Also emits structured boot/shutdown events so ops dashboards can detect
    crash loops vs. clean restarts.
    """
    settings = get_settings()
    logger.info(
        "app.startup",
        env=settings.APP_ENV,
        version=settings.APP_VERSION,
    )
    try:
        app.state.redis_pool = ConnectionPool.from_url(
            str(settings.REDIS_URL),
            decode_responses=True,
        )
    except Exception as exc:
        logger.critical("redis.pool_init_failed", url=str(settings.REDIS_URL), exc_info=exc)
        raise

    consumer_task = asyncio.create_task(event_consumer_loop())

    try:
        yield
    finally:
        logger.info("app.shutdown")
        consumer_task.cancel()
        # Cancellation is expected; any other error has already been logged
        # inside the loop. We must not let teardown raise.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await consumer_task
        try:
            await app.state.redis_pool.aclose()
        except Exception:
            logger.warning("redis.pool_disconnect_failed", exc_info=True)


def create_app() -> FastAPI:
    """Build a fully-wired FastAPI instance.

    Factored out so tests can construct fresh app instances with overridden
    settings without re-importing the module.
    """
    configure_logging()  # Must run before any logger.* call below.
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        docs_url="/docs" if settings.APP_ENV != "prod" else None,
        redoc_url=None,
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        lifespan=_lifespan,
    )

    # --- Middleware (order matters — added bottom-up at runtime) -------------
    app.middleware("http")(correlation_id_middleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Routers --------------------------------------------------------------
    app.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)

    # --- Error handlers (always last so they override any defaults) ----------
    register_exception_handlers(app)

    return app


app: FastAPI = create_app()
