"""Tests for ``app.workers.audit_cleanup.cleanup_old_audit_logs``.

The cleanup task deletes ``audit_logs`` rows older than the configured
retention (``Settings.AUDIT_LOG_RETENTION_DAYS``, default 90) in batches
of :data:`_DELETE_BATCH_SIZE` to avoid long row locks.

Strategy: seed rows directly via the project's transactional
``db_session`` fixture (gives us a clean DB per test), then patch the
task's ``SessionLocal`` to hand it the same session. Commits inside the
task land in the savepoint that the fixture restarts on the outer
rollback, so the seeded + task-written state is observable from the
test thread but doesn't leak to other tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.workers import audit_cleanup
from app.workers.audit_cleanup import cleanup_old_audit_logs
from sqlalchemy import select
from sqlalchemy.orm import Session


def _seed_audit_row(db: Session, *, created_at: datetime) -> AuditLog:
    """Insert one ``audit_logs`` row with an explicit ``created_at``.

    ``Base.created_at`` is a server-default column, so SQLAlchemy ignores
    a plain attribute assignment on a fresh instance until we flush; the
    explicit ``db.execute`` here overrides the default. Using a raw INSERT
    avoids the ``onupdate``/``server_default`` interplay that would force
    a second UPDATE for each row.
    """
    row = AuditLog(
        action="test.action",
        user_id=None,
        resource_type="test",
        resource_id=uuid.uuid4(),
        old_value=None,
        new_value={"seeded": True},
    )
    db.add(row)
    db.flush()
    # Override the server-default created_at. Once flushed the row has an
    # id we can target; this is the cleanest way to backdate a test row
    # without dropping the server_default on the column.
    row.created_at = created_at
    db.flush()
    return row


@pytest.fixture
def _patch_session_local(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ``audit_cleanup.SessionLocal()`` to the test's transactional session.

    The task closes the session in its ``finally``; we shadow ``close()``
    so the fixture's own cleanup still runs the outer rollback. Without
    this, the task would close the connection mid-test and the fixture
    teardown would explode.
    """
    db_session.close = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(audit_cleanup, "SessionLocal", lambda: db_session)


def test_cleanup_deletes_rows_older_than_retention(
    db_session: Session,
    _patch_session_local: None,
) -> None:
    """Mix of old + recent rows → only the old ones are deleted.

    Default retention is 90 days. Seed 3 rows at 100 days old and 3 rows
    at 1 day old, run the task, assert only the recent 3 survive.
    """
    now = datetime.now(UTC)
    settings = get_settings()
    old_cutoff = now - timedelta(days=settings.AUDIT_LOG_RETENTION_DAYS + 10)
    recent = now - timedelta(days=1)

    for _ in range(3):
        _seed_audit_row(db_session, created_at=old_cutoff)
    recent_ids = {_seed_audit_row(db_session, created_at=recent).id for _ in range(3)}
    db_session.commit()

    deleted = cleanup_old_audit_logs()

    assert deleted == 3
    surviving = set(db_session.execute(select(AuditLog.id)).scalars().all())
    assert surviving == recent_ids


def test_cleanup_no_op_on_empty_table(
    db_session: Session,
    _patch_session_local: None,
) -> None:
    """Empty audit_logs → returns 0 without error.

    The loop's first DELETE returns 0 rows so the task exits cleanly on
    the first iteration. No infinite loop, no exception, no commit.
    """
    # Sanity check: table is empty (db_session is a fresh transactional
    # nesting). If anything leaks across tests this assertion will catch
    # it before the cleanup call masks the regression.
    existing = db_session.execute(select(AuditLog.id)).scalars().all()
    assert list(existing) == []

    deleted = cleanup_old_audit_logs()
    assert deleted == 0


def test_cleanup_respects_batch_size(
    db_session: Session,
    _patch_session_local: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2500 expired rows → at least three batches are committed.

    Patches ``_DELETE_BATCH_SIZE`` down to a small value so the test does
    not need to insert thousands of rows. We then assert the loop runs
    more than once by counting how many times ``Session.commit`` is
    invoked. A single-pass implementation would commit at most once.
    """
    settings = get_settings()
    old = datetime.now(UTC) - timedelta(days=settings.AUDIT_LOG_RETENTION_DAYS + 5)
    for _ in range(25):
        _seed_audit_row(db_session, created_at=old)
    db_session.commit()

    # Force tiny batches so we exercise the loop instead of the single-shot path.
    monkeypatch.setattr(audit_cleanup, "_DELETE_BATCH_SIZE", 10)

    commit_calls = {"n": 0}
    original_commit = db_session.commit

    def _counting_commit() -> None:
        commit_calls["n"] += 1
        original_commit()

    monkeypatch.setattr(db_session, "commit", _counting_commit)

    deleted = cleanup_old_audit_logs()

    # 25 rows / batch_size 10 → 3 batches (10 + 10 + 5).
    assert deleted == 25
    assert commit_calls["n"] >= 3, (
        f"expected >= 3 commits (batched deletes), got {commit_calls['n']}"
    )


def test_cleanup_uses_configured_retention_days(
    db_session: Session,
    _patch_session_local: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AUDIT_LOG_RETENTION_DAYS`` controls the cutoff at runtime.

    Set retention to 1 day, seed one row at 25h old and one at 23h old,
    assert only the 25h row is deleted. Pins the contract: future code
    must not hardcode the 90-day default.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "AUDIT_LOG_RETENTION_DAYS", 1)

    now = datetime.now(UTC)
    twenty_five_hours_ago = now - timedelta(hours=25)
    twenty_three_hours_ago = now - timedelta(hours=23)

    old_row = _seed_audit_row(db_session, created_at=twenty_five_hours_ago)
    survivor = _seed_audit_row(db_session, created_at=twenty_three_hours_ago)
    db_session.commit()

    deleted = cleanup_old_audit_logs()

    assert deleted == 1
    surviving_ids = set(db_session.execute(select(AuditLog.id)).scalars().all())
    assert old_row.id not in surviving_ids
    assert survivor.id in surviving_ids
