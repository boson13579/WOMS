"""Unified API error handling.

Per docs/RULES.md §4 and the project's API design guide, every error response — no
matter where it originates (validation, route handler, unexpected exception) —
must conform to a single envelope:

    {
      "error": {
        "code": <int>,            // HTTP status code
        "message": <str>,         // human-readable summary
        "details": [ ... ]        // optional list of structured detail entries
      }
    }

Returning identical shapes for 400/404/422/500 etc. lets the frontend write a
single error renderer instead of branching on every endpoint's quirks.

Note: We deliberately wrap FastAPI's default validation responses (422) and
the catch-all `Exception` handler (500) so nothing leaks the legacy
`{"detail": ...}` shape.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)


def _envelope(
    code: int,
    message: str,
    details: list[Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical error envelope."""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
        }
    }


async def http_exception_handler(
    _request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Convert any HTTPException (Starlette or FastAPI) into the envelope.

    Registered against `StarletteHTTPException` so it covers Starlette's own
    404-on-no-route as well as FastAPI's `HTTPException` (which subclasses it).
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(
            code=exc.status_code,
            message=str(exc.detail),
        ),
        headers=exc.headers,
    )


async def validation_exception_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Convert Pydantic / FastAPI request validation errors (HTTP 422).

    Pydantic produces structured details (loc, msg, type) which we forward
    verbatim — they're invaluable for frontend form-field highlighting.

    ``exc.errors()`` can include non-JSON-safe objects inside ``ctx`` (e.g.
    pydantic stuffs the raw ``ValueError`` instance under ``ctx["error"]``
    when a custom validator raises). ``jsonable_encoder`` recursively
    converts those to JSON-safe types — without it, ``JSONResponse.render``
    blows up with ``TypeError: Object of type ValueError is not JSON
    serializable`` and the handler ends up tripping the 500 catch-all.
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_envelope(
            code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            message="Request validation failed.",
            details=jsonable_encoder(exc.errors()),
        ),
    )


async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Catch-all for anything not derived from `HTTPException`.

    We log the full stack trace with structlog (so it lands in our ECS-formatted
    audit pipeline) but return a *generic* message to the client to avoid
    leaking internal details (table names, file paths, secrets in tracebacks).
    """
    logger.error(
        "api.unhandled_exception",
        exc_info=exc,
        path=str(request.url.path),
        method=request.method,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message="Internal server error.",
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all handlers to a FastAPI app instance.

    Called once from `app.main` during startup wiring. Order matters only for
    the catch-all `Exception` — FastAPI dispatches the most-specific handler
    first regardless.
    """
    # Starlette types these handlers as `Callable[..., Exception]`, so the
    # narrower signatures we use trip mypy. The dispatch is correct at runtime
    # — FastAPI calls each handler only with its declared exception type.
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
