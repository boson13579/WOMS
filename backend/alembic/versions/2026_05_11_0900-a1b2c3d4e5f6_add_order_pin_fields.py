"""add_order_pin_fields

Revision ID: a1b2c3d4e5f6
Revises: 848e0ae420d8
Create Date: 2026-05-11 09:00:00.000000+00:00

Adds the three pin-related columns spec'd in docs/scheduling.md §pinning:

* ``pinned_production_date`` (Date, NULL) — the user-requested forced
  production day, only set when ``is_pinned`` is true.
* ``is_pinned`` (Bool, default false) — production pin: forces the order to
  ``pinned_production_date`` regardless of EDF.
* ``is_processing_locked`` (Bool, default false) — editing-lock pin: true
  while an op for this order is in the scheduler queue. Frontend uses it to
  disable edits on the row.

All three default to "no pin" so existing rows remain semantically unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "848e0ae420d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("pinned_production_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column(
            "is_pinned",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "is_processing_locked",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "is_processing_locked")
    op.drop_column("orders", "is_pinned")
    op.drop_column("orders", "pinned_production_date")
