"""add_audit_logs_created_at_index

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-05-20 09:00:00.000000+00:00

Adds an index on ``audit_logs.created_at`` so the daily retention cleanup
(``app.workers.audit_cleanup.cleanup_old_audit_logs``) can prune rows by
``WHERE created_at < cutoff`` without table-scanning. The audit feed
endpoint ``GET /audit`` already paginates on ``created_at DESC`` and will
also benefit from the index.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_logs_created_at",
        "audit_logs",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
