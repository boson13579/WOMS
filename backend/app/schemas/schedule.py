"""Pydantic DTOs for the scheduling endpoints."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.order import OrderStatus

__all__ = [
    "DailyAssignment",
    "ScheduleOperationRequest",
    "ScheduleOperationResponse",
    "ScheduleRebuildResponse",
    "ScheduleResultResponse",
    "ScheduleStatusResponse",
    "ScheduleTriggerResponse",
]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ScheduleOperationRequest(BaseModel):
    """One pending op to push onto the scheduler queue.

    The Order CRUD layer fires one of these for every create / cancel, and a
    pair (``remove`` then ``add``) for every quantity / deadline edit.

    The ``group`` field decides processing order in the worker:

    - ``"shrink"`` — ops that *free* capacity: pure ``remove`` (delete), the
      remove+add pair for a deadline deferral, or the remove+add pair for a
      quantity decrease.
    - ``"grow"`` — ops that *consume* capacity: pure ``add``, the remove+add
      pair for a deadline advance, or the remove+add pair for a quantity
      increase.

    Worker drains all shrink-group ops first (FIFO), then all grow-group ops
    (FIFO). This keeps each compound update atomic *within* its group and
    avoids growing-half ops failing on capacity that's still occupied by the
    shrinking-half of a different update.

    For compound updates the producer must tag *both* the remove and the add
    with the same group; for single ops (pure add / pure remove) ``group``
    can be omitted and the ``mode="before"`` validator fills in the obvious
    default before field validation, so the field type itself is non-Optional.
    """

    op: Literal["add", "remove"]
    group: Literal["shrink", "grow"]
    order_id: uuid.UUID
    order_number: str
    wafer_quantity: int = Field(gt=0)
    deadline: date
    requested_by: uuid.UUID

    @model_validator(mode="before")
    @classmethod
    def _default_group_from_op(cls, data: Any) -> Any:
        """Inject the degenerate ``group`` when caller omits it.

        Runs before per-field validation so the field can be declared as
        ``Literal["shrink", "grow"]`` (non-Optional). Only meaningful for
        single ops; compound updates MUST tag ``group`` explicitly on both
        halves — the default is wrong for them.
        """
        if isinstance(data, dict) and data.get("group") is None:
            op = data.get("op")
            if op == "remove":
                data = {**data, "group": "shrink"}
            elif op == "add":
                data = {**data, "group": "grow"}
        return data


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ScheduleTriggerResponse(BaseModel):
    """Returned by ``POST /schedule/trigger`` after dispatching a Celery task."""

    task_id: str
    message: str


class ScheduleOperationResponse(BaseModel):
    """Returned by ``POST /schedule/operations`` after the op is enqueued.

    Op processing is async (Celery worker drains the queue), so this response
    just confirms the op landed in Redis. The caller should not block on the
    schedule actually being applied — that will arrive via the
    ``schedule.updated`` WebSocket broadcast.
    """

    message: str


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
