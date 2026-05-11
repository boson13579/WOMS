"""Pydantic DTOs for the scheduling endpoints."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.order import OrderStatus

__all__ = [
    "DailyAssignment",
    "ScheduleCompoundFailedDetail",
    "ScheduleCompoundRequest",
    "ScheduleCompoundResponse",
    "ScheduleOpInCompound",
    "ScheduleRebuildResponse",
    "ScheduleResultResponse",
    "ScheduleStatusResponse",
    "ScheduleTriggerResponse",
]


# ---------------------------------------------------------------------------
# Request schemas — compound flow
# ---------------------------------------------------------------------------


class ScheduleOpInCompound(BaseModel):
    """One leaf op inside a :class:`ScheduleCompoundRequest`.

    Op kinds:

    - ``"add"`` / ``"remove"`` — push an order into / out of the pq.
    - ``"pin"`` — lock an order to a specific ``fake_deadline`` (must be
      ≤ real deadline). Requires ``fake_deadline``.
    - ``"unpin"`` — release a pinned order back to the pq.
      ``fake_deadline`` must NOT be supplied.

    ``group`` and ``requested_by`` live at the compound level (not here) —
    every op in a compound shares them, so duplicating them on each leaf
    would just create chances for them to disagree.
    """

    op: Literal["add", "remove", "pin", "unpin"]
    order_id: uuid.UUID
    order_number: str
    wafer_quantity: int = Field(gt=0)
    deadline: date
    fake_deadline: date | None = None

    @model_validator(mode="after")
    def _pin_requires_fake_deadline(self) -> ScheduleOpInCompound:
        """``pin`` ops MUST include ``fake_deadline``; other ops MUST NOT."""
        if self.op == "pin" and self.fake_deadline is None:
            raise ValueError("op='pin' requires fake_deadline")
        if self.op != "pin" and self.fake_deadline is not None:
            raise ValueError(
                f"op={self.op!r} must NOT include fake_deadline; only 'pin' uses it"
            )
        return self


class ScheduleCompoundRequest(BaseModel):
    """One atomic business action against the scheduler.

    A compound is a list of leaf ops that the worker processes as a single
    atomic unit. The full sequence of ops either completes successfully or
    the worker snapshot-rollbacks ``SchedulerState`` to its pre-compound
    state and emits ``schedule.compound_failed`` over WebSocket — no
    partial successes leaking past the compound boundary.

    Why compound instead of per-op:

    - **Atomic from the outside**: ``schedule:status`` stays ``running`` for
      the full compound. ``advance_day_task`` / ``rebuild_schedule_task``
      can't slip in mid-compound, eliminating the per-op race where a
      compound's [unpin, remove, add] sequence got split across an
      advance_day boundary.
    - **Saga rollback on failure**: producer never has to deal with
      "remove succeeded but add failed, the order is now destroyed";
      worker undoes earlier successes so state matches the pre-compound
      world.
    - **Aligns with how producers think**: a PATCH that defers a pinned
      order is one business action containing 3-4 worker ops; modelling it
      as one Redis member matches that reality.

    Producer responsibility:

    - Pick a single ``group`` for the whole compound. The compound is
      scored as shrink (sorted before grow) or grow (sorted after shrink)
      and worker pops one compound per ``run_scheduling_task`` invocation.
    - Order the ops correctly: pin/unpin lifecycle ops must precede the
      modify ops they bracket. E.g. modifying a pinned order's deadline:
      ``[unpin, remove(old), add(new), pin(same day)]`` — wrong order will
      fail at one of the membership guards (worker rolls back, WS fires).
    - Ensure ``compound_id`` is unique. Cancellations / status queries
      use it to address a specific in-flight compound.
    """

    compound_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    group: Literal["shrink", "grow"]
    ops: list[ScheduleOpInCompound] = Field(min_length=1)
    requested_by: uuid.UUID

    @model_validator(mode="after")
    def _ops_target_same_order_within_compound(self) -> ScheduleCompoundRequest:
        """Best-effort sanity check: ops in a compound usually target one order.

        Not strictly required — a multi-order batch action would in principle
        be representable as one compound. But in this codebase every compound
        flows from a single Order-CRUD action, so a multi-order compound is
        almost certainly a bug. Warn (raise) loudly.
        """
        if not self.ops:
            return self
        order_ids = {op.order_id for op in self.ops}
        if len(order_ids) > 1:
            raise ValueError(
                "All ops in a compound must target the same order_id; "
                f"got {len(order_ids)} distinct ids."
            )
        return self


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ScheduleTriggerResponse(BaseModel):
    """Returned by ``POST /schedule/trigger`` after dispatching a Celery task."""

    task_id: str
    message: str


class ScheduleCompoundResponse(BaseModel):
    """Returned by ``POST /schedule/operations`` after a compound is enqueued.

    The compound runs async (worker drains the queue), so this response just
    confirms the compound landed in Redis. The caller observes outcome via:
    - ``schedule.updated`` WebSocket broadcast on success.
    - ``schedule.compound_failed`` WebSocket (``schedule:notify_user`` to
      ``requested_by``) on failure-with-rollback.
    """

    compound_id: uuid.UUID
    message: str


class ScheduleCompoundFailedDetail(BaseModel):
    """Payload of the ``schedule.compound_failed`` WebSocket event.

    Documented as a schema so the frontend has a structured contract to
    code against. The worker constructs an instance and serializes it into
    the WS envelope's ``message`` field; the field names below match.
    """

    type: Literal["schedule.compound_failed"] = "schedule.compound_failed"
    compound_id: uuid.UUID
    # 0-indexed position inside ``ops``. ``ops[failed_op_index]`` is the
    # one that returned non-success.
    failed_op_index: int
    failed_op: Literal["add", "remove", "pin", "unpin"]
    order_id: uuid.UUID
    order_number: str
    # ``ScheduleResult.status`` of the failed leaf op.
    reason: str
    detail: str | None = None
    # Always ``True`` for compound failures — present so the frontend can
    # surface "your previous state has been restored" reliably.
    rolled_back: Literal[True] = True


class ScheduleStatusResponse(BaseModel):
    """Lifecycle snapshot of the scheduling worker, mirrored from Redis."""

    state: Literal["idle", "running", "failed"]
    started_at: str | None = None
    finished_at: str | None = None
    task_id: str | None = None
    error: str | None = None
    # Populated only when there is no Redis status doc (e.g., first deploy).
    message: str | None = None


class DailyAssignment(BaseModel):
    """One day's slice of a (potentially multi-day) order assignment."""

    date: date
    quantity: int = Field(gt=0)


class ScheduleRebuildResponse(BaseModel):
    """Returned by ``POST /schedule/rebuild`` after dispatching the task.

    Rebuild is async — the endpoint returns immediately with the Celery
    task ID. Skipped orders (deadline outside the 30-day horizon) and the
    final ``schedule.updated`` notification arrive via WebSocket once the
    task body finishes its wait + rebuild + re-trigger sequence.
    """

    task_id: str
    message: str


class ScheduleResultResponse(BaseModel):
    """One row of the materialized schedule (an order in ``scheduled`` status).

    ``scheduled_production_date`` / ``expected_delivery_date`` summarize
    earliest / latest production day; ``daily_breakdown`` lists the per-day
    quantities for orders that span multiple days. The breakdown is empty
    when no scheduler state is available (e.g., first deploy before any run).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_number: str
    customer_name: str
    wafer_quantity: int
    requested_delivery_date: date
    scheduled_production_date: date | None
    expected_delivery_date: date | None
    status: OrderStatus
    daily_breakdown: list[DailyAssignment] = Field(default_factory=list)
