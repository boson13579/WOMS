"""make_email_primary

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-13 10:00:00.000000+00:00

Makes ``users.email`` a primary contact field: NOT NULL, UNIQUE, indexed.
Rows with NULL email are backfilled with ``username || '@placeholder.internal'``
before the NOT NULL constraint is applied.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE users SET email = username || '@placeholder.internal' WHERE email IS NULL")
    op.alter_column("users", "email", nullable=False, existing_type=sa.String(254))
    op.create_index("ix_users_email", "users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.alter_column("users", "email", nullable=True, existing_type=sa.String(254))
