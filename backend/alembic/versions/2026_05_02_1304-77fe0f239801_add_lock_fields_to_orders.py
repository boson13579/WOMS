"""add_lock_fields_to_orders

Revision ID: 77fe0f239801
Revises: 848e0ae420d8
Create Date: 2026-05-02 13:04:29.242634+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "77fe0f239801"
down_revision: str | None = "848e0ae420d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders", sa.Column("is_locked", sa.Boolean(), server_default="false", nullable=False)
    )
    op.add_column("orders", sa.Column("locked_by", sa.UUID(), nullable=True))
    op.add_column("orders", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("soft_pin_date", sa.Date(), nullable=True))
    op.create_foreign_key("fk_orders_locked_by_users", "orders", "users", ["locked_by"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_orders_locked_by_users", "orders", type_="foreignkey")
    op.drop_column("orders", "soft_pin_date")
    op.drop_column("orders", "locked_at")
    op.drop_column("orders", "locked_by")
    op.drop_column("orders", "is_locked")
