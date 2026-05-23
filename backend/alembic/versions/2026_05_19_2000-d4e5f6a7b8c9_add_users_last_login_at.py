"""add_users_last_login_at

Revision ID: d4e5f6a7b8c9
Revises: 3eb23c0bbcd4
Create Date: 2026-05-19 20:00:00.000000+00:00

Adds a nullable ``last_login_at`` timestamp column to the ``users`` table so
the auth service can stamp the most recent successful login per user.
Read on user-detail pages only — never filtered/sorted by in MVP, so no
index is created.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "3eb23c0bbcd4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
