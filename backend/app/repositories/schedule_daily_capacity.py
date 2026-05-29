"""Repository for the ``schedule_daily_capacity`` aggregate snapshot.

Per RULES.md: zero business logic here, just CRUD. The cache-replacement
write pattern (truncate-and-insert) lives in :func:`replace_all` because
that's a single SQL contract — pushing it to the service layer would
just be a wrapper around the same two statements.

Read sites: ``GET /schedule/capacity-usage`` (one call to
:func:`get_all_ordered`).
Write site: ``app/services/order.py::apply_schedule`` (one call to
:func:`replace_all` per materialization).
"""

from __future__ import annotations

from datetime import date as date_type

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.schedule_daily_capacity import ScheduleDailyCapacity

__all__ = ["get_all_ordered", "replace_all"]


def get_all_ordered(db: Session) -> list[ScheduleDailyCapacity]:
    """Fetch every snapshot row, ordered by date ascending.

    Always returns a list (possibly empty). Soft-delete filter is applied
    even though we never soft-delete in practice — keeps the repo
    convention uniform and protects against accidental future writes that
    do mark a row deleted.
    """
    stmt = (
        select(ScheduleDailyCapacity)
        .where(ScheduleDailyCapacity.is_deleted.is_(False))
        .order_by(ScheduleDailyCapacity.date.asc())
    )
    return list(db.scalars(stmt).all())


def replace_all(
    db: Session,
    *,
    entries: dict[date_type, int],
    preserve_date: date_type | None = None,
) -> int:
    """Atomic truncate-and-insert: wipe every snapshot row, then insert ``entries``.

    Why truncate-and-insert instead of upsert: the snapshot is a complete
    re-projection of ``compute_schedule()`` output. Any pre-existing row
    that isn't in the new ``entries`` is stale (e.g. yesterday's row
    after an ``advance_day_task``, or a previously-occupied date that the
    new schedule pushed earlier). Upserting would leak those stale rows
    forever; we'd then need a separate "delete dates not in the new set"
    step, which is exactly the truncate we're already doing.

    Issues a ``DELETE`` even when ``entries`` is empty so a "schedule
    cleared to nothing" call also clears the snapshot. ``flush()`` (not
    ``commit()``) — caller decides the transaction boundary.

    **``preserve_date``** (typically the scheduler's current
    ``base_date`` = "today"): when set, the existing snapshot row for
    that date is **NOT** wiped, and any ``entries`` key matching that
    date is **NOT** inserted (which would overwrite the preserved row).
    Use case: ``materialize_schedule_task`` / ``rebuild_schedule_task``
    only see future-day work in their ``compute_schedule`` output —
    ``base_date`` itself is empty under the "tree day 1 = base_date +
    1" rule. The today snapshot row is written by
    ``advance_day_task``'s ``apply_schedule`` (which gets the
    ``today_portion`` entries) and must survive subsequent materializer
    passes, otherwise "today's in_production wafer usage" disappears
    from ``GET /schedule/capacity-usage`` after the next materialize.
    ``advance_day_task`` passes ``preserve_date=None`` so its own
    today-row write goes through normally.

    Returns the number of rows inserted (excluding any preserved row).
    """
    if preserve_date is not None:
        db.execute(delete(ScheduleDailyCapacity).where(ScheduleDailyCapacity.date != preserve_date))
    else:
        db.execute(delete(ScheduleDailyCapacity))
    if not entries:
        db.flush()
        return 0

    rows = [
        ScheduleDailyCapacity(date=d, used_quantity=q)
        for d, q in sorted(entries.items())
        if q > 0 and d != preserve_date
    ]
    db.add_all(rows)
    db.flush()
    return len(rows)
