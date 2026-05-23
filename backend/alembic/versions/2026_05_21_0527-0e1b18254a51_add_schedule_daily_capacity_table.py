"""add schedule_daily_capacity table

Revision ID: 0e1b18254a51
Revises: e5f6a7b8c9d0
Create Date: 2026-05-21 05:27:04.317466+00:00

Adds a per-day capacity-usage cache populated by
``apply_schedule``. One row per date. See
``app/models/schedule_daily_capacity.py`` for design notes on why this
inherits the Base columns but doesn't semantically use ``id`` /
``version_id`` / ``is_deleted``.

Note: autogen also wanted to ``drop_index(ix_audit_logs_created_at)``
because that index isn't declared on the ``AuditLog`` model (drift from
the manual migration ``e5f6a7b8c9d0_add_audit_logs_created_at_index``).
Removed from this migration — that drift is a separate concern; we don't
want this PR to silently drop a production index.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0e1b18254a51"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedule_daily_capacity",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("used_quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("version_id", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", name="uq_schedule_daily_capacity_date"),
    )
    op.create_index(
        op.f("ix_schedule_daily_capacity_date"),
        "schedule_daily_capacity",
        ["date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_schedule_daily_capacity_is_deleted"),
        "schedule_daily_capacity",
        ["is_deleted"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_schedule_daily_capacity_is_deleted"),
        table_name="schedule_daily_capacity",
    )
    op.drop_index(
        op.f("ix_schedule_daily_capacity_date"),
        table_name="schedule_daily_capacity",
    )
    op.drop_table("schedule_daily_capacity")
