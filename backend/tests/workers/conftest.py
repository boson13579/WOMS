"""Worker-test bootstrap.

Worker tests import ``app.workers.scheduling`` at module load, which in turn
loads ``app.core.config``. The shared ``tests/conftest.py`` only sets
``DATABASE_URL``/``REDIS_URL``/``JWT_SECRET`` inside the ``postgres_container``
fixture, so worker tests run in isolation (e.g. ``pytest tests/workers/``)
would fail config validation before the first test even starts.

Setting the env vars at conftest *module level* — not inside a fixture —
guarantees they are present before pytest collects the worker test files.
``setdefault`` keeps the values harmless when the postgres container has
already populated the real ones.
"""

from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://test:test@localhost/test",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod")
