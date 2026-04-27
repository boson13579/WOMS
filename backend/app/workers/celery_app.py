"""Celery application instance.

Phase 1 ships only the wiring — no actual tasks yet. Phase 2 will add
`schedule_orders`, `send_notification`, etc. as siblings.

Start a worker with:
    celery -A app.workers.celery_app worker --loglevel=INFO
"""

from __future__ import annotations

from celery import Celery

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
)

# Auto-discovery hook for future task modules. Add modules to this list as
# Phase 2 features land (e.g., `"app.workers.scheduling"`).
celery_app.autodiscover_tasks(packages=["app.workers"])
