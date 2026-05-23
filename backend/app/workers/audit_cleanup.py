"""Daily prune of ``audit_logs`` rows older than the configured retention.

Audit log is a write-heavy append-only stream — without retention it
grows unboundedly, slowing the operator-facing ``GET /audit`` feed and
bloating backups. We delete in batches (LIMIT 1000) to avoid long row
locks that would block fresh audit writes from in-flight requests, then
loop until no rows older than the cutoff remain.

Runs once a day via Celery Beat (see :mod:`app.workers.celery_app`).
The task is idempotent: running it multiple times within the same day
just deletes nothing on the second pass.

Retention is governed by ``Settings.AUDIT_LOG_RETENTION_DAYS`` (default
90 days). Override per environment via the ``AUDIT_LOG_RETENTION_DAYS``
env var.

The migration ``2026_05_20_..._add_audit_logs_created_at_index.py`` adds
an index on ``audit_logs.created_at`` so the ``WHERE created_at < cutoff``
filter doesn't table-scan a multi-million-row table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.audit_log import AuditLog
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

# Batch size for the iterative DELETE. Small enough that each statement
# completes inside a few hundred milliseconds even on a busy DB, large
# enough that draining a backlog of millions of rows does not take all
# day. Postgres locks rows (not pages) so the batches do not block fresh
# audit inserts on other rows.
_DELETE_BATCH_SIZE = 1000


@celery_app.task(name="audit.cleanup_old_logs")  # type: ignore[untyped-decorator]
def cleanup_old_audit_logs() -> int:
    """Delete ``audit_logs`` rows older than the retention setting.

    Returns the total number of rows deleted (useful for testing and for
    the Celery result backend's audit trail of cleanup runs).

    Implementation notes:
      * Cutoff is ``now - AUDIT_LOG_RETENTION_DAYS`` in UTC. We use UTC
        explicitly because ``audit_logs.created_at`` carries a timezone
        and mixing naive / aware datetimes raises ``TypeError`` from
        SQLAlchemy.
      * The DELETE statement uses an ``id IN (SELECT id ... LIMIT N)``
        subquery instead of ``DELETE ... LIMIT N`` because Postgres
        forbids ``LIMIT`` directly on ``DELETE``. The subquery is the
        idiomatic workaround.
      * Each batch is committed before the next runs so a crash mid-loop
        does not roll back all the deletions performed so far.
    """
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=settings.AUDIT_LOG_RETENTION_DAYS)
    deleted_total = 0
    db = SessionLocal()
    try:
        while True:
            # Two-step subquery: pick a batch of expired ids first, then
            # delete by those ids. Postgres rejects ``DELETE ... LIMIT N``
            # directly, so this is the standard workaround.
            id_subq = (
                select(AuditLog.id)
                .where(AuditLog.created_at < cutoff)
                .limit(_DELETE_BATCH_SIZE)
                .subquery()
            )
            result = db.execute(delete(AuditLog).where(AuditLog.id.in_(select(id_subq))))
            # ``CursorResult.rowcount`` is what SQLAlchemy returns for an
            # ORM-style ``delete()`` execution; the broader ``Result`` type
            # the engine narrows to does not advertise the attribute so we
            # silence the false-positive narrowing complaint here.
            count = result.rowcount or 0  # type: ignore[attr-defined]
            if count == 0:
                break
            db.commit()
            deleted_total += count
        logger.info(
            "audit.cleanup.completed",
            deleted=deleted_total,
            cutoff=cutoff.isoformat(),
            retention_days=settings.AUDIT_LOG_RETENTION_DAYS,
        )
        return deleted_total
    finally:
        db.close()


__all__ = ["cleanup_old_audit_logs"]
