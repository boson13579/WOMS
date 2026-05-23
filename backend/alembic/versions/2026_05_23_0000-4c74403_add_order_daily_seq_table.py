"""add order_daily_seq table

Revision ID: 4c74403fix01
Revises: 0e1b18254a51
Create Date: 2026-05-23 00:00:00.000000+00:00

Replaces the COUNT(*)-based order number generation with an atomic
upsert counter. Each row tracks the last allocated sequence number for
a given date, eliminating the TOCTOU race condition that caused
duplicate order_number generation under concurrent load.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4c74403fix01"
down_revision: str | None = "0e1b18254a51"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_daily_seq",
        sa.Column("date", sa.Date(), primary_key=True, nullable=False),
        sa.Column(
            "last_seq",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("order_daily_seq")
