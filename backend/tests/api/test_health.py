"""TDD demo: the canonical health-check endpoint.

This file is the project's reference TDD example. It walks through the three
phases prescribed by docs/RULES.md §5:

    [RED]      Test written first, fails because nothing exists yet.
    [GREEN]    Minimum implementation to make the test pass.
    [REFACTOR] (Skipped here — endpoint is already trivial.)

When adding new endpoints, follow this same structure: write the test in
`tests/api/test_<feature>.py`, run pytest (it should fail), then implement
the route in `app/api/v1/<feature>.py` until the test goes green.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# [RED] phase — these assertions were written before health.py existed and
# pinned down the exact contract: status 200 + body == {"status": "ok"}.
# ---------------------------------------------------------------------------
def test_health_returns_200(client: TestClient) -> None:
    """Liveness probe must return HTTP 200."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200


def test_health_returns_ok_payload(client: TestClient) -> None:
    """Body shape is fixed by contract; frontend depends on `status == "ok"`."""
    response = client.get("/api/v1/health")
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# [GREEN] is provided by `app/api/v1/health.py`. Both tests pass once the
# router is wired up in `app/api/v1/__init__.py` and included in `main.py`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bonus — verify the unified error envelope (`app.api.errors`) is active by
# probing a route that doesn't exist. This guards against accidental removal
# of the global exception handlers.
# ---------------------------------------------------------------------------
def test_unknown_route_uses_unified_error_envelope(client: TestClient) -> None:
    """404 responses must conform to the canonical `{"error": {...}}` shape."""
    response = client.get("/api/v1/this-route-does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == 404
    assert isinstance(body["error"]["message"], str)
    assert isinstance(body["error"]["details"], list)
