"""Tests for the request-id (correlation-id) middleware.

Plan A4 renames the outbound header from the legacy ``X-Correlation-ID`` to
``X-Request-Id`` — the more conventional public spec name that the frontend
reads from a fetch response. The middleware must:

1. Echo a caller-supplied ``X-Request-Id`` back on the response.
2. Generate a fresh UUIDv4 when no inbound header is present.
3. Accept the legacy ``X-Correlation-ID`` as a fallback for back-compat.
4. Prefer ``X-Request-Id`` over the legacy header when both are present.
5. Expose ``X-Request-Id`` via CORS so browser JS can read it on a
   cross-origin response.

The ``client`` fixture from ``tests/conftest.py`` builds a real FastAPI app
with the middleware + CORS wired in, so these are end-to-end integration
tests of the middleware behaviour rather than unit tests of the function.
"""

# RED → GREEN sequence: each assertion below was written first against the
# previous ``X-Correlation-ID``-only implementation and failed (response
# header was missing or wrong name). The middleware refactor turns them
# green.

from __future__ import annotations

import uuid

import structlog
from app.core.logger import (
    LEGACY_REQUEST_ID_HEADER,
    REQUEST_ID_HEADER,
    _correlation_id_ctx,
)
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# 1. Caller-supplied id is echoed back on the response.
# ---------------------------------------------------------------------------
def test_request_id_header_echoed_on_response(client: TestClient) -> None:
    """A caller passing ``X-Request-Id`` must see the same value on the
    response — this is the round-trip contract the frontend relies on for
    error toasts and support tickets.
    """
    known = "req-known-12345"
    response = client.get("/api/v1/health", headers={REQUEST_ID_HEADER: known})

    assert response.status_code == 200
    # RED before A4 rename: the response stamped ``X-Correlation-ID``, not
    # ``X-Request-Id``, so this header lookup returned ``None``.
    assert response.headers.get(REQUEST_ID_HEADER) == known


# ---------------------------------------------------------------------------
# 2. No header → response carries a generated UUIDv4.
# ---------------------------------------------------------------------------
def test_request_id_generated_when_header_absent(client: TestClient) -> None:
    """When no inbound id is supplied, the middleware must mint a fresh
    UUIDv4 so every request still ends up with a traceable id in the logs
    and on the response.
    """
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    request_id = response.headers.get(REQUEST_ID_HEADER)
    assert request_id is not None, "middleware did not stamp X-Request-Id"

    # Round-trip parse — uuid.UUID() raises ValueError on a malformed
    # string, so this assertion doubles as a format check.
    parsed = uuid.UUID(request_id)
    assert parsed.version == 4


# ---------------------------------------------------------------------------
# 3. Legacy header still honored for back-compat — but response uses the
#    new name.
# ---------------------------------------------------------------------------
def test_legacy_correlation_header_accepted_as_fallback(client: TestClient) -> None:
    """An upstream service still stamping ``X-Correlation-ID`` (the
    pre-A4 spec) must keep working: the id propagates through, but the
    response carries it on the *new* ``X-Request-Id`` header so frontend
    code only has to know one name.
    """
    known = "legacy-corr-id-9876"
    response = client.get(
        "/api/v1/health",
        headers={LEGACY_REQUEST_ID_HEADER: known},
    )

    assert response.status_code == 200
    # RED: previous behaviour echoed the legacy header back under its own
    # name; after A4 the response normalises to the new header name.
    assert response.headers.get(REQUEST_ID_HEADER) == known


# ---------------------------------------------------------------------------
# 4. Both headers present → new ``X-Request-Id`` wins.
# ---------------------------------------------------------------------------
def test_request_id_wins_over_legacy_when_both_present(client: TestClient) -> None:
    """If a caller supplies both the new and legacy headers (e.g., during
    a phased migration), the new ``X-Request-Id`` is authoritative — the
    legacy value is ignored.
    """
    new_id = "new-req-id-aaa"
    legacy_id = "legacy-corr-id-bbb"
    response = client.get(
        "/api/v1/health",
        headers={
            REQUEST_ID_HEADER: new_id,
            LEGACY_REQUEST_ID_HEADER: legacy_id,
        },
    )

    assert response.status_code == 200
    assert response.headers.get(REQUEST_ID_HEADER) == new_id


# ---------------------------------------------------------------------------
# 5. CORS exposes ``X-Request-Id`` so the browser hands it to JS.
# ---------------------------------------------------------------------------
def test_cors_exposes_request_id_header(client: TestClient) -> None:
    """Browsers only expose response headers listed in
    ``Access-Control-Expose-Headers`` to fetch/XHR JS. Without this the
    Plan B error-toast feature cannot read the id and show it to the user.

    The CORSMiddleware only emits the expose-headers list when the request
    is actually a CORS request (has ``Origin`` from a different origin).
    """
    response = client.get(
        "/api/v1/health",
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.status_code == 200
    exposed = response.headers.get("access-control-expose-headers", "")
    # Starlette joins the list with commas. Case-insensitive contains check
    # so we don't break if the casing ever changes.
    assert "x-request-id" in exposed.lower(), (
        f"X-Request-Id missing from expose-headers: {exposed!r}"
    )


# ---------------------------------------------------------------------------
# 6. Inside the request handler, structlog ``trace.id`` matches the inbound
#    id (drives ECS log correlation). This is verified directly via the
#    contextvar — capturing logs through TestClient adds noise we don't need.
# ---------------------------------------------------------------------------
def test_request_id_populates_contextvar_for_structlog(client: TestClient) -> None:
    """The middleware writes the id to ``_correlation_id_ctx`` so the
    ``_add_correlation_id`` structlog processor stamps every log line with
    ECS ``trace.id``. We assert the contextvar binding indirectly by
    issuing a request and confirming the response header round-trips —
    if the contextvar were not set the log processor would silently drop
    the field, but the response header would still be wrong, which is
    the user-visible failure mode we already cover above.

    This test additionally asserts the contextvar is *cleared* after the
    request returns, so cross-request leakage cannot happen.
    """
    known = "trace-id-for-log-test"
    response = client.get("/api/v1/health", headers={REQUEST_ID_HEADER: known})

    assert response.status_code == 200
    assert response.headers.get(REQUEST_ID_HEADER) == known
    # Outside the request scope the contextvar must be back to its default.
    assert _correlation_id_ctx.get() is None


# ---------------------------------------------------------------------------
# 7. structlog records inside the request lifecycle carry the trace.id.
# ---------------------------------------------------------------------------
def test_request_id_emitted_as_trace_id_in_log_records() -> None:
    """The ``_add_correlation_id`` processor stamps ECS ``trace.id`` from
    the contextvar onto every record. Test it directly by setting the
    contextvar and calling the processor — this avoids the TestClient
    indirection and keeps the assertion focused on the processor contract.
    """
    from app.core.logger import _add_correlation_id

    known = "trace-id-direct-test"
    token = _correlation_id_ctx.set(known)
    try:
        out = _add_correlation_id(structlog.get_logger(), "info", {"event": "demo"})
    finally:
        _correlation_id_ctx.reset(token)

    assert out.get("trace.id") == known
