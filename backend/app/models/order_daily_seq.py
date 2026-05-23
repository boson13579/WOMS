"""Counter table for daily order sequence numbers.

A plain SQLAlchemy Table (not a mapped class) registered into Base.metadata
so that Base.metadata.create_all() picks it up in tests. The table intentionally
has no id/version_id/is_deleted columns — it is a pure counter, not a domain
entity.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.models.base_class import Base

order_daily_seq = sa.Table(
    "order_daily_seq",
    Base.metadata,
    sa.Column("date", sa.Date(), primary_key=True, nullable=False),
    sa.Column("last_seq", sa.Integer(), server_default="0", nullable=False),
)
