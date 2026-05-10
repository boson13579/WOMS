"""FastAPI application factory.

This module is the *only* place where the HTTP layer, error handlers, logging,
and middleware are wired together. Keeping the wiring centralized makes the
boot sequence auditable and unit-testable.

Run with:  `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import ConnectionPool

from app.api.errors import register_exception_handlers
from app.api.v1 import api_router as api_v1_router
from app.core.config import get_settings
from app.core.logger import configure_logging, correlation_id_middleware

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks.

    We log structured boot/shutdown events so ops dashboards can detect crash
    loops vs. clean restarts.
    """
    settings = get_settings()
    logger.info(
        "app.startup",
        env=settings.APP_ENV,
        version=settings.APP_VERSION,
    )
    app.state.redis_pool = ConnectionPool.from_url(
        str(settings.REDIS_URL),
        decode_responses=True,
    )
    yield
    await app.state.redis_pool.disconnect()
    logger.info("app.shutdown")


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
