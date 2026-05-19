"""Cross-layer audit helper — stdout event + optional dual-write to DB.

This module is the single entrypoint every business-logic call-site uses to
record an auditable action. Two functions are exposed:

* :func:`event_log` — pure stdout signal (ECS-compatible structlog), with no
  side effects on the database. Use for cross-cutting notifications and
  state-transition signals that don't warrant a persistent audit row.

* :func:`record_audit` — *dual-write*: emit the stdout event AND insert a
  row into ``audit_logs``. The DB insert is wrapped in a SAVEPOINT
  (``Session.begin_nested``) so that a transient insert failure (FK race,
  serialization error, ...) rolls back ONLY the audit row — the parent
  business transaction's session stays usable and the caller's outer
  ``db.commit()`` of the real work still succeeds. The stdout event is
  always emitted first so we never lose the signal entirely, even if the
  DB write fails.

Callers DO NOT commit inside ``record_audit``; they remain responsible for
``db.commit()`` of the surrounding business operation, exactly as today.

Why this lives in ``app.core`` and not under ``app.services``: the helper
is consumed by services AND workers and binds together stdout-logging
(``app.core.logger``) with the SQLAlchemy repository layer. It has no
domain logic of its own — only cross-cutting plumbing — which is the
charter of ``core``.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.repositories import audit_log as audit_log_repo

logger = structlog.get_logger("audit")

__all__ = ["event_log", "record_audit"]


def event_log(
    *,
    action: str,
    actor_id: str | None,
    resource_type: str,
    resource_id: str,
    changes: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    """Emit one ECS-compliant audit record to stdout — no DB write.

    Use for pure event signals: state transitions, scheduling notifications,
    internal lifecycle events that don't need a persistent trail. For
    resource mutations that *must* be recoverable from Postgres alone use
    :func:`record_audit` instead.

    Args:
        action: Dotted event name, e.g. ``"order.created"``.
        actor_id: User UUID who performed the action (None for system actions).
        resource_type: Domain entity name (``"order"``, ``"user"``, ...).
        resource_id: Primary key of the affected resource (string form).
        changes: Optional diff payload merged into the record as ``changes``.
        **extra: Free-form additional fields merged into the record.
    """
    logger.info(
        "audit",
        **{
            "event.action": action,
            "event.category": "audit",
            "event.kind": "event",
            "user.id": actor_id,
            "resource.type": resource_type,
            "resource.id": resource_id,
            "changes": changes or {},
            **extra,
        },
    )


def record_audit(
    db: Session,
    *,
    action: str,
    actor_id: uuid.UUID | None,
    resource_type: str,
    resource_id: uuid.UUID,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    """Dual-write: stdout event + ``audit_logs`` row in one call.

    Behaviour:

    1. Emit the stdout event first (via :func:`event_log`) — this is
       unconditional and never raises.
    2. Open a SAVEPOINT (``db.begin_nested()``) and call
       ``audit_log_repo.create()`` inside it. If the insert raises, the
       SAVEPOINT rolls back, the parent session stays usable, and we log a
       warning. No exception propagates to the caller.

    The SAVEPOINT is required because plain SQLAlchemy sessions are
    invalidated by any DB error — so naively catching the exception in the
    parent transaction would cause the caller's subsequent ``db.commit()``
    to raise ``PendingRollbackError``. Wrapping in ``begin_nested`` confines
    the failure to the audit row alone, preserving "the audit row is
    best-effort" semantics without breaking the business commit.

    Callers MUST still call ``db.commit()`` themselves after this returns
    — :func:`record_audit` does not commit on its own.

    Args:
        db: Active SQLAlchemy session for the surrounding business tx.
        action: Dotted event name.
        actor_id: UUID of the acting user (None for system-driven events
            like scheduler-applied schedules).
        resource_type: Domain entity name.
        resource_id: Primary key of the affected resource.
        old_value: Pre-mutation snapshot (dict or None).
        new_value: Post-mutation snapshot (dict or None).
        **extra: Free-form additional fields forwarded to the stdout record.
    """
    event_log(
        action=action,
        actor_id=str(actor_id) if actor_id is not None else None,
        resource_type=resource_type,
        resource_id=str(resource_id),
        changes={"old": old_value, "new": new_value},
        **extra,
    )
    try:
        with db.begin_nested():
            audit_log_repo.create(
                db,
                action=action,
                user_id=actor_id,
                resource_type=resource_type,
                resource_id=resource_id,
                old_value=old_value,
                new_value=new_value,
            )
    except Exception as exc:
        # SAVEPOINT rolled back; parent session stays usable. We still
        # logged the event to stdout above, so the signal is not lost.
        logger.warning(
            "audit.record.db_failure",
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            error=str(exc),
            exc_info=True,
        )
