"""Smoke test: the Celery app actually registers every scheduling task.

Why this matters
----------------
``celery_app.autodiscover_tasks(packages=["app.workers"])`` only finds tasks
inside a per-package ``tasks.py`` module. The scheduling tasks live in
``app/workers/scheduling.py``, so without an explicit ``imports=...`` entry
the production worker process imports ``celery_app`` (which doesn't
transitively touch ``app.workers.scheduling``), no @task decorator fires,
and every ``.delay()`` call later pushes a task name the worker doesn't
know — silently dead-lettering the whole scheduling pipeline.

The in-process pytest suite still passes in that broken configuration
because pytest imports the test module → which imports the worker module →
which runs the decorators. So this guard test is the only thing that
exercises the same path a real ``celery -A app.workers.celery_app worker``
startup would.
"""

from __future__ import annotations


def test_celery_registers_all_scheduling_tasks() -> None:
    # Import the celery_app fresh (no transitive ``app.workers.scheduling``
    # import path) to mirror what ``celery -A ... worker`` does.
    from app.workers.celery_app import celery_app

    expected = {
        "scheduling.run",
        "scheduling.advance_day",
        "scheduling.rebuild",
        "scheduling.materialize",
    }
    registered = set(celery_app.tasks.keys())
    missing = expected - registered
    assert not missing, (
        f"Celery is missing scheduling tasks: {missing}. "
        "Check celery_app.conf.update(imports=...) covers app.workers.scheduling."
    )
