"""add_order_daily_breakdown

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-12 09:00:00.000000+00:00

Adds the ``daily_breakdown`` JSONB column on ``orders`` so the materializer
can persist the per-day quantity split alongside the earliest/latest summary.
Lets ``GET /schedule/result`` answer from DB alone, no on-the-fly
``compute_schedule(state)`` call needed (the Redis state is now used only for
algorithm operations, not for read-path).

Shape per row::

    [
      {"date": "2026-05-12", "quantity": 6000},
      {"date": "2026-05-13", "quantity": 4000}
    ]

Sorted ascending by date. NULL means "not currently scheduled" — same
semantic as ``scheduled_production_date IS NULL``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "daily_breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "daily_breakdown")
