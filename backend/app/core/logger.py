"""Structured JSON logging — Elastic Common Schema (ECS) compatible.

Per docs/RULES.md §1, logs are an *event stream*: stdout-only, JSON, machine-parsable.
Per PRD §1.6 we must produce an audit trail for every CRUD operation, including
actor, action, resource, and a before/after diff.

We achieve both with `structlog`:
  * `configure_logging()` installs ECS-compatible processors so every record
    carries `@timestamp`, `service.name`, `log.level`, and a `correlation_id`
    propagated across async boundaries via a `ContextVar`.
  * `correlation_id_middleware` accepts an inbound `X-Request-Id` header (or
    legacy `X-Correlation-ID` for back-compat) or generates a UUID, stores it
    in the contextvar, and stamps it onto the outbound response — so a single
    request can be traced from frontend log through Celery worker logs.

ECS field reference: https://www.elastic.co/guide/en/ecs/current/index.html
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog
from fastapi import Request, Response
from structlog.types import EventDict, Processor

from app.core.config import get_settings

# ---------------------------------------------------------------------------
# Correlation-ID context variable.
# ContextVars are async-safe: each FastAPI request and Celery task gets its
# own copy, so multi-tenant logs never bleed identifiers across requests.
# ---------------------------------------------------------------------------
_correlation_id_ctx: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Outbound spec name the frontend reads (Plan A4 rename — see
# notes/agent_progress/Observability_plan_a_backend.md §A4). Both the request
# header lookup *and* the response stamp use this name.
REQUEST_ID_HEADER = "X-Request-Id"
# Legacy inbound header — still honored so older clients / load balancers /
# upstream services that haven't migrated yet can keep stamping trace ids.
# The response is always emitted under `REQUEST_ID_HEADER`.
LEGACY_REQUEST_ID_HEADER = "X-Correlation-ID"
# Backwards-compatible alias kept for any code/tests that still import the
# old name. New call sites should prefer ``REQUEST_ID_HEADER``.
CORRELATION_HEADER = REQUEST_ID_HEADER


# ---------------------------------------------------------------------------
# Custom structlog processors — translate native fields into ECS names.
# ---------------------------------------------------------------------------
def _add_correlation_id(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
    """Inject the current correlation_id into every log record."""
    cid = _correlation_id_ctx.get()
    if cid is not None:
        # ECS: `trace.id` is the canonical field for request correlation.
        event_dict.setdefault("trace.id", cid)
    return event_dict


def _add_service_metadata(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
    """Attach static `service.name` / `service.version` / `service.environment`."""
    settings = get_settings()
    event_dict.setdefault("service.name", settings.APP_NAME)
    event_dict.setdefault("service.version", settings.APP_VERSION)
    event_dict.setdefault("service.environment", settings.APP_ENV)
    return event_dict


def _rename_event_to_message(
    _logger: object,
    _method: str,
    event_dict: EventDict,
) -> EventDict:
    """ECS uses `message` rather than structlog's default `event` key."""
    if "event" in event_dict and "message" not in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


# ---------------------------------------------------------------------------
# Public configuration entrypoint.
# ---------------------------------------------------------------------------
def configure_logging() -> None:
    """Configure structlog + stdlib logging to emit ECS JSON to stdout.

    Idempotent: safe to call repeatedly (e.g., from tests).
    """
    settings = get_settings()
    level = getattr(logging, settings.LOG_LEVEL)

    # Shared processor chain — applied to BOTH structlog calls and stdlib
    # records (uvicorn, sqlalchemy, alembic) so the entire process emits a
    # single uniform stream.
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="@timestamp"),
        _add_correlation_id,
        _add_service_metadata,
        _rename_event_to_message,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # JSON formatter for stdlib handlers — ensures uvicorn/sqlalchemy log
    # lines come out in the same shape as our structlog calls.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace any pre-existing handlers (e.g., uvicorn's default) so output
    # doesn't appear twice in pretty + JSON form.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet down noisy third-party loggers.
    for noisy in ("uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Correlation-ID middleware (for FastAPI).
# ---------------------------------------------------------------------------
async def correlation_id_middleware(
    request: Request,
    call_next: Any,
) -> Response:
    """Propagate a request ID across the request lifecycle.

    Prefers the inbound ``X-Request-Id`` header (the public spec name the
    frontend reads). Falls back to the legacy ``X-Correlation-ID`` header so
    older clients / upstream services that haven't migrated yet keep working.
    Otherwise generates a fresh UUIDv4.

    The chosen id is bound to the ``correlation_id`` contextvar (so structlog
    emits it under ECS ``trace.id``) and *always* stamped onto the outbound
    response under ``X-Request-Id`` — even when the inbound used the legacy
    header — so clients can rely on a single response header name.
    """
    incoming = request.headers.get(REQUEST_ID_HEADER) or request.headers.get(
        LEGACY_REQUEST_ID_HEADER
    )
    cid = incoming or str(uuid.uuid4())
    token = _correlation_id_ctx.set(cid)
    try:
        response: Response = await call_next(request)
    finally:
        _correlation_id_ctx.reset(token)
    response.headers[REQUEST_ID_HEADER] = cid
    return response
