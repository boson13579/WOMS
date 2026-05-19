"""Unit tests for the cross-layer audit helper at ``app.core.audit``.

The helper exposes two entrypoints:

* ``event_log()`` — pure stdout signal (structlog), no DB.
* ``record_audit()`` — dual-write: stdout signal **and** an ``audit_logs``
  row. The DB write is wrapped in a SAVEPOINT (``db.begin_nested()``) so a
  transient insert failure does not poison the parent session — the caller's
  surrounding ``db.commit()`` of the business operation still succeeds.

The DB-touching tests rely on the project ``db_session`` fixture which
already nests its own SAVEPOINT around each test. ``record_audit``'s inner
``db.begin_nested()`` therefore opens a *nested-nested* savepoint — which is
exactly what SQLAlchemy supports and what we want to assert here.
"""

# RED → GREEN sequence: each test was written failing first against an absent
# implementation, then turned green by the code under test. Markers below show
# which assertion line was the original red.

from __future__ import annotations

import uuid
from typing import Any

import pytest
import structlog
from app.core import audit as audit_module
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole
from sqlalchemy import select
from sqlalchemy.orm import Session


def _make_user(db: Session, *, username: str) -> User:
    """Insert a minimal user so audit rows have a valid actor reference."""
    import bcrypt

    user = User(
        username=username,
        email=f"{username}@test.internal",
        password_hash=bcrypt.hashpw(b"password123", bcrypt.gensalt()).decode(),
        role=UserRole.scheduler,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# event_log
# ---------------------------------------------------------------------------


def test_event_log_emits_structlog_event_with_ecs_keys(db_session: Session) -> None:
    """``event_log`` must emit one structlog record with ECS-shaped keys and
    must NOT touch the DB.
    """
    actor_id = str(uuid.uuid4())
    resource_id = str(uuid.uuid4())

    with structlog.testing.capture_logs() as captured:
        audit_module.event_log(
            action="order.demo",
            actor_id=actor_id,
            resource_type="order",
            resource_id=resource_id,
            changes={"old": None, "new": {"x": 1}},
        )

    # RED: event_log did not exist → captured was empty → len(captured) == 0.
    assert len(captured) == 1
    record = captured[0]
    assert record["event"] == "audit"
    assert record["event.action"] == "order.demo"
    assert record["event.category"] == "audit"
    assert record["event.kind"] == "event"
    assert record["user.id"] == actor_id
    assert record["resource.type"] == "order"
    assert record["resource.id"] == resource_id
    assert record["changes"] == {"old": None, "new": {"x": 1}}

    # Belt-and-suspenders: no audit_logs row was written.
    rows = db_session.scalars(select(AuditLog)).all()
    assert list(rows) == []


def test_event_log_accepts_none_actor(db_session: Session) -> None:
    """System-driven events (e.g. scheduler) must pass ``actor_id=None``."""
    with structlog.testing.capture_logs() as captured:
        audit_module.event_log(
            action="order.scheduled",
            actor_id=None,
            resource_type="order",
            resource_id=str(uuid.uuid4()),
        )

    # RED: event_log raised on actor_id=None before the None-is-system contract landed.
    assert len(captured) == 1
    assert captured[0]["user.id"] is None


# ---------------------------------------------------------------------------
# record_audit — happy path
# ---------------------------------------------------------------------------


def test_record_audit_writes_db_row_and_emits_event(db_session: Session) -> None:
    """``record_audit`` must do BOTH a DB insert and a structlog emit."""
    actor = _make_user(db_session, username="audit-helper-actor")
    resource_id = uuid.uuid4()
    old_value: dict[str, Any] = {"quantity": 100}
    new_value: dict[str, Any] = {"quantity": 200}

    with structlog.testing.capture_logs() as captured:
        audit_module.record_audit(
            db_session,
            action="order.updated",
            actor_id=actor.id,
            resource_type="order",
            resource_id=resource_id,
            old_value=old_value,
            new_value=new_value,
        )

    # stdout side
    # RED: record_audit did not exist → AttributeError raised before any capture.
    assert len(captured) == 1
    record = captured[0]
    assert record["event.action"] == "order.updated"
    assert record["user.id"] == str(actor.id)
    assert record["resource.id"] == str(resource_id)
    assert record["changes"] == {"old": old_value, "new": new_value}

    # DB side — row visible inside the open transaction.
    rows = list(
        db_session.scalars(select(AuditLog).where(AuditLog.resource_id == resource_id)).all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "order.updated"
    assert row.user_id == actor.id
    assert row.resource_type == "order"
    assert row.old_value == old_value
    assert row.new_value == new_value


def test_record_audit_writes_null_user_id_for_system_actor(db_session: Session) -> None:
    """System-driven calls (e.g. scheduler) pass ``actor_id=None`` — the
    resulting DB row must have ``user_id IS NULL`` and the emitted record
    must carry ``user.id=None``.
    """
    resource_id = uuid.uuid4()

    with structlog.testing.capture_logs() as captured:
        audit_module.record_audit(
            db_session,
            action="order.scheduled",
            actor_id=None,
            resource_type="order",
            resource_id=resource_id,
            new_value={"scheduled_production_date": "2026-05-20"},
        )

    # RED: pre-impl record_audit lacked the None→system mapping; emitted user.id was missing.
    assert captured[0]["user.id"] is None

    row = db_session.scalars(select(AuditLog).where(AuditLog.resource_id == resource_id)).one()
    assert row.user_id is None


def test_record_audit_does_not_commit(db_session: Session) -> None:
    """The helper must NOT commit — callers control the outer transaction."""
    actor = _make_user(db_session, username="audit-helper-nocommit")
    resource_id = uuid.uuid4()

    audit_module.record_audit(
        db_session,
        action="order.created",
        actor_id=actor.id,
        resource_type="order",
        resource_id=resource_id,
        new_value={"customer_name": "X"},
    )

    # Roll back the SAVEPOINT-wrapped session (db_session fixture restarts
    # the savepoint on after_transaction_end). If record_audit had committed,
    # the row would survive the rollback.
    db_session.rollback()

    rows = db_session.scalars(select(AuditLog).where(AuditLog.resource_id == resource_id)).all()
    # RED: early record_audit drafts called db.commit() internally → row survived rollback.
    assert list(rows) == []


# ---------------------------------------------------------------------------
# record_audit — failure semantics
# ---------------------------------------------------------------------------


def test_record_audit_db_failure_does_not_poison_parent_session(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the DB write inside the SAVEPOINT raises, the parent session must
    stay usable so the caller's ``db.commit()`` of the surrounding business
    operation still succeeds.

    Without ``db.begin_nested()``, SQLAlchemy would mark the session as
    invalidated after the error and any subsequent ``db.commit()`` would
    raise ``PendingRollbackError``. The SAVEPOINT wrap is what keeps the
    parent transaction alive.
    """
    actor = _make_user(db_session, username="audit-helper-savepoint")
    resource_id = uuid.uuid4()

    # Force the repo insert to blow up. record_audit must catch this inside
    # its SAVEPOINT block and emit a warning.
    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated DB blow-up")

    monkeypatch.setattr(audit_module.audit_log_repo, "create", _raise)

    with structlog.testing.capture_logs() as captured:
        audit_module.record_audit(
            db_session,
            action="order.created",
            actor_id=actor.id,
            resource_type="order",
            resource_id=resource_id,
            new_value={"x": 1},
        )

    # The audit event still made it to stdout — never silently dropped.
    # RED: without SAVEPOINT, the raised RuntimeError escaped record_audit and no event was captured.
    event_records = [r for r in captured if r.get("event") == "audit"]
    assert len(event_records) == 1
    assert event_records[0]["event.action"] == "order.created"

    # A warning was logged so the failure is observable.
    warning_records = [r for r in captured if r.get("log_level") == "warning"]
    assert any("audit.record.db_failure" in r.get("event", "") for r in warning_records)

    # Parent session is still usable — do a real write + commit. If the
    # session were poisoned this commit would raise PendingRollbackError.
    other_user = User(
        username="audit-helper-post-failure",
        email="post-failure@test.internal",
        password_hash="hash",
        role=UserRole.viewer,
        is_active=True,
    )
    db_session.add(other_user)
    db_session.commit()  # MUST NOT raise.

    # And the bad audit row was NOT written.
    rows = db_session.scalars(select(AuditLog).where(AuditLog.resource_id == resource_id)).all()
    assert list(rows) == []
