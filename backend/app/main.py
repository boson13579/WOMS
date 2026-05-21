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

from app.api.errors import register_exception_handlers
from app.api.v1 import api_router as api_v1_router
from app.api.v1.websocket import event_consumer_loop
from app.core.config import get_settings
from app.core.logger import configure_logging, correlation_id_middleware
from app.core.red_metrics import red_metrics_middleware
from app.services.startup_recovery import run_startup_recovery

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

    # Best-effort scheduler-state recovery: catch up missed advance_day
    # ticks, rebuild on missing state, kick orphan pending ops. Safe to
    # run unconditionally — has its own NX-lock so multi-replica deploys
    # only execute on one replica per restart event.
    run_startup_recovery()

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
    # FastAPI executes ``middleware("http")`` registrations in REVERSE order
    # of registration: the *last* one registered runs FIRST on the way in
    # and LAST on the way out. We want ``correlation_id_middleware`` to run
    # first (so every downstream log carries ``trace.id``), so it must be
    # registered LAST among the HTTP middlewares.
    #
    # ``red_metrics_middleware`` is inside ``correlation_id_middleware`` so
    # the contextvar is already populated when samples are recorded — any
    # warning log emitted by the Redis writer carries the same trace id as
    # the request that produced it.
    app.middleware("http")(red_metrics_middleware)
    app.middleware("http")(correlation_id_middleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # ``X-Request-Id`` is emitted by ``correlation_id_middleware`` on every
        # response. Browsers hide all response headers by default; without
        # ``expose_headers`` frontend code cannot read it via
        # ``res.headers.get('X-Request-Id')`` from a fetch response. Plan B's
        # error toast surfaces this id to support, so it must be exposed.
        expose_headers=["X-Request-Id"],
    )

    # --- Routers --------------------------------------------------------------
    app.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)

    # --- Error handlers (always last so they override any defaults) ----------
    register_exception_handlers(app)

    return app


app: FastAPI = create_app()
