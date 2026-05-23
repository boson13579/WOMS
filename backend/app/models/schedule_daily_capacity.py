"""Per-day used-capacity snapshot, materialized by ``apply_schedule``.

This table is a **denormalized aggregate** of every scheduled order's
``daily_breakdown[d].quantity`` summed across orders for each date ``d``.
It exists because ``GET /schedule/capacity-usage`` wants to answer
"given the EDF forward-fill plan, how many wafers are committed on each
of the next 30 days?" in a single fast lookup — aggregating the JSONB
arrays across the orders table at read time is feasible but adds latency
to a dashboard endpoint that gets polled a lot.

Write site: ``app/services/order.py::apply_schedule`` is the **only**
writer. It iterates the freshly-computed ``compute_schedule()`` result
to build per-order ``daily_breakdown`` payloads anyway; in the same pass
it accumulates per-day totals and bulk-writes the snapshot. Single
writer in a single transaction means snapshot + per-order breakdown can
never drift — the two views of the same schedule are committed atomically.

Read site: ``GET /schedule/capacity-usage``. Reads 30 rows (one per day
in the horizon) and computes ``remaining = DAILY_CAPACITY - used`` in the
response layer. Storing ``remaining`` would double the data without
adding value because ``used`` plus the constant suffices.

**Not a true domain entity**: this table is a cache of derived data, not
a primary record. We don't inherit the project's audit columns
(``id``/``version_id``/``is_deleted``) because they're meaningless here:

- soft-delete: rows are wholesale replaced every materialization, never
  marked deleted
- optimistic-lock: there's a single writer (``apply_schedule``) running
  inside the worker's state-writer lock, so concurrent writes are already
  serialized at a layer above
- UUID id: ``date`` is the natural primary key (one row per day, ever)

Adding those columns would force us to either (1) maintain UUIDs that
nothing references, or (2) bypass the convention with an ad-hoc UNIQUE
on ``date`` and a useless surrogate id. We pick "deviate from the
convention with an inline comment" over either of those.
"""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base_class import Base


class ScheduleDailyCapacity(Base):
    """One row per calendar day with the total wafers planned for that day.

    ``date`` is the natural PK. ``used_quantity`` is the sum of every
    scheduled order's ``daily_breakdown[d].quantity`` for that date; the
    remaining capacity at read time is ``DAILY_CAPACITY - used_quantity``.

    The inherited :class:`Base` columns (``id``, ``version_id``,
    ``is_deleted``) are unused for this aggregate cache (see the module
    docstring). They're left as defaults to keep Alembic auto-generation
    matching the rest of the schema without us hand-editing migrations.

    **Lag note for consumers of ``/schedule/capacity-usage``**: this table
    reflects the last ``compute_schedule()`` output committed by
    ``materialize_schedule_task``. Compounds that have been accepted by
    the worker fast-path but haven't yet been materialized into DB will
    NOT show up in ``used_quantity`` — there's a one-materialize-cycle
    lag (typically 50ms-2s) between "compound accepted" and "snapshot
    reflects it". Frontend should treat the values as "best known plan
    as of the last materialize", not real-time.
    """

    __tablename__ = "schedule_daily_capacity"
    __table_args__ = (sa.UniqueConstraint("date", name="uq_schedule_daily_capacity_date"),)

    # Override the version_id-based optimistic lock inherited from ``Base``.
    # The lock is meaningful for orders / users / audit logs where multiple
    # writers can race. This aggregate cache has a single writer
    # (``replace_all`` in ``apply_schedule``) running inside the worker's
    # ``schedule:state_writer_lock`` so concurrent writes are already
    # serialized one layer up. The current ``replace_all`` (DELETE + INSERT)
    # never triggers the version check, but the moment someone writes an
    # ``UPDATE schedule_daily_capacity SET ... WHERE date=...`` it will
    # silently fail with ``StaleDataError`` — and that's exactly the kind
    # of session-poisoning bug we fixed in ``apply_schedule`` for the
    # orders table. Disable the lock explicitly here to keep future
    # writers safe-by-default.
    __mapper_args__ = {}  # noqa: RUF012

    date: Mapped[date] = mapped_column(
        sa.Date,
        nullable=False,
        index=True,
    )
    used_quantity: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
