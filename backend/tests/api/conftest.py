"""Shared fixtures for ``tests/api/*``.

Most API endpoint tests assert HTTP-layer behavior (status codes, request
shape validation, role gates) and don't care about the side-effect of
pushing a compound onto the Redis-backed scheduler queue. This module
installs an **autouse** mock for ``enqueue_compound`` so order CRUD endpoints
that internally invoke it don't fail when no live Redis is around.

Tests that DO want to inspect the queue (``test_schedule.py``'s
``/operations`` cases, ``test_orders.py``'s smart-routing cases) override
the mock locally by re-monkeypatching the appropriate import-site.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _autouse_mock_enqueue_compound(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub ``schedule_queue.enqueue_compound`` at every call-site by default.

    The order service imports it as ``from ... import enqueue_compound`` —
    once that import has happened, monkeypatching the source module isn't
    enough; we have to patch the symbol where it's been bound. Same applies
    to the schedule API endpoint.

    Returns the mock so tests can opt-in to assertions on it by re-querying
    the fixture (``def test_x(_autouse_mock_enqueue_compound): ...``).
    """
    mock = MagicMock()
    # Order CRUD service imports the symbol directly into its namespace.
    monkeypatch.setattr("app.services.order.enqueue_compound", mock)
    # Schedule API router does likewise.
    monkeypatch.setattr("app.api.v1.schedule.enqueue_compound", mock)
    return mock
