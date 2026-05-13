"""Shared fixtures for ``tests/workers``.

The worker tasks ``advance_day_task`` and ``rebuild_schedule_task`` end
their bodies by dispatching ``materialize_schedule_task.delay()`` so the
caller observes the post-advance/rebuild ``daily_breakdown`` immediately
(bounding the stale-write window from the lock-free materializer
refactor). That ``.delay()`` call hits the Celery broker — in tests we
don't want a real dispatch, and CI doesn't have a broker reachable from
the test process at all (Redis runs as a testcontainer on a random port,
but the Celery app captured ``REDIS_URL=redis://localhost:6379/0`` at
import time).

Autouse-patching ``.delay`` here removes the dispatch from every worker
test by default. Tests that assert on the dispatch (like
``test_advance_day_dispatches_materializer_after_commit``) override the
patch locally — ``monkeypatch.setattr`` stacks, with the test-local
override winning.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _mock_materialize_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.workers.scheduling.materialize_schedule_task.delay",
        MagicMock(),
    )
