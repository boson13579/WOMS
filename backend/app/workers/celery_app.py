"""Celery application instance.

Phase 1 ships only the wiring — no actual tasks yet. Phase 2 will add
`schedule_orders`, `send_notification`, etc. as siblings.

Start a worker with:
    celery -A app.workers.celery_app worker --loglevel=INFO
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings
from app.core.logger import configure_logging

# Ensure structured logging is set up before Celery's own logger initializes.
configure_logging()

settings = get_settings()

celery_app = Celery(
    "smart-order-worker",
    broker=settings.celery_broker,
    backend=settings.celery_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,  # re-deliver if a worker dies mid-task
    worker_prefetch_multiplier=1,  # prevents one slow task from blocking others
    # Explicit import of task modules. ``autodiscover_tasks`` only finds
    # tasks declared in a per-package ``tasks.py``; our scheduling tasks
    # live in ``app/workers/scheduling.py`` so they'd silently fail to
    # register in a real worker process (the in-process test suite still
    # works because pytest imports the module directly, but a deployed
    # ``celery -A ... worker`` wouldn't). Listing the module here forces
    # Celery to import it at worker startup so the @task decorators fire.
    imports=(
        "app.workers.scheduling",
        "app.workers.audit_cleanup",
    ),
)

# Auto-discovery hook kept for future per-package tasks.py modules. The
# explicit ``imports`` above is the source of truth for current tasks.
celery_app.autodiscover_tasks(packages=["app.workers"])

# Periodic-task schedule consumed by ``celery -A app.workers.celery_app beat``.
# Times are in UTC (matches ``timezone="UTC"`` above). Keep the list small
# and easy to read — each entry is one well-defined operational concern.
celery_app.conf.beat_schedule = {
    # Daily prune of audit_logs older than ``AUDIT_LOG_RETENTION_DAYS``.
    # Runs at 02:00 UTC so it does not contend with the existing
    # ``advance_day`` work that scheduling tasks trigger at midnight UTC.
    "audit-log-cleanup": {
        "task": "audit.cleanup_old_logs",
        "schedule": crontab(hour="2", minute="0"),
    },
}
