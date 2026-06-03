"""Compound ``db_action`` finalize path — shared by worker and cancel-endpoint.

Background
----------
``app/services/order.py``'s producer functions (``create_order`` /
``update_order`` / ``delete_order``) make a partial DB write before
enqueueing a compound:

* ``create_order`` INSERTs the row with ``is_processing_locked=True``
* ``update_order`` sets ``status=pending`` + ``is_processing_locked=True``
* ``delete_order`` sets ``is_processing_locked=True``

The COMPLETION of those writes (writing new column values, soft-deleting
the row, clearing the lock, emitting the right audit row) is **deferred
to the worker** so that DB and ``SchedulerState`` stay consistent — the
producer doesn't know whether the algorithm will accept or reject the
compound, so it can't commit the user-visible change until the worker
has done its admission check.

This module owns the deferred-finalize logic. There are TWO callers:

1. ``app/workers/scheduling.py::_commit_accepted_batch`` calls it with
   ``accepted=True`` for compounds the algorithm accepted, and
   ``accepted=False`` for compounds that hit a batch-admission rejection
   or per-leaf invariant break.
2. ``app/services/schedule_queue.py::cancel_compound`` calls it with
   ``accepted=False`` when the user retracts a queued compound — the
   compound never reaches the worker, but the producer's pre-write
   still has to be unwound. Without this call the row would stay stuck
   in ``is_processing_locked=True`` forever (the bug the function fixes).

Either way the semantics are identical: ``accepted=True`` finalizes the
user's intended change; ``accepted=False`` compensates the producer's
pre-write back to a sane resting state.
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import SessionLocal
from app.models.order import Order, OrderStatus
from app.services import notification as notification_service
from app.services.schedule_lag import record_lag_sample

logger = structlog.get_logger(__name__)

__all__ = [
    "perform_compound_db_action",
]


def perform_compound_db_action(
    compound: dict[str, Any],
    *,
    accepted: bool,
) -> None:
    """Execute the compound's ``db_action`` after state has settled.

    Idempotent at the row level: if the order disappeared between
    enqueue and execution (e.g. a follow-up DELETE landed first), this
    function logs a warning and returns without raising.

    Branch matrix:

    ``kind="create"``:
      - accepted: clear the in-flight lock (producer pre-created the
        row; materializer will fill scheduling cols).
      - rejected: clear lock; set ``status=cancelled``; keep
        ``is_deleted`` unchanged/``False`` so the row remains visible
        in user-facing queries; emit ``order.cancelled`` audit record.

    ``kind="update"``:
      - accepted: write the new ``wafer_quantity`` /
        ``requested_delivery_date`` / ``notes`` / ``assigned_to`` from
        ``db_action.new_*``; clear lock; emit ``order.updated`` audit
        record with the diff.
      - rejected: clear lock; restore ``status`` to ``scheduled`` if
        the row has a ``scheduled_production_date`` (which means it
        was already accepted into the schedule before this PATCH), or
        ``pending`` otherwise. DB columns themselves stay at pre-PATCH
        values because the producer never wrote them.

    ``kind="delete"``:
      - accepted: soft-delete (``is_deleted=True``,
        ``status=cancelled``); clear lock; emit ``order.deleted``
        audit record.
      - rejected: clear lock; status restoration mirrors update.

    ``kind="cancel"`` (user clicks the "取消訂單" button on a scheduled
    order — the row stays visible but stops occupying capacity):
      - accepted: ``status=cancelled`` but ``is_deleted`` stays
        ``False`` so the row remains in ``GET /orders``; clear lock;
        emit ``order.cancelled`` audit record.
      - rejected: clear lock; status restoration mirrors update.

    Same scheduler-side compound shape as ``delete``
    (``[remove]`` / ``[unpin, remove]``); the only difference between
    the two accept-paths is whether ``is_deleted`` flips, and the
    audit label (``order.deleted`` vs ``order.cancelled``).
    """
    db_action_raw = compound.get("db_action")
    if not db_action_raw:
        return  # legacy / internally-generated compound with no db_action

    kind = db_action_raw["kind"]
    actor_id_raw = db_action_raw["actor_id"]
    actor_id = uuid.UUID(actor_id_raw) if actor_id_raw else None

    ops = compound.get("ops") or []
    if not ops:
        logger.warning("schedule.db_action.no_ops", compound_id=compound.get("compound_id"))
        return
    # All ops in a producer-generated compound target the same order_id.
    order_id = uuid.UUID(ops[0]["order_id"])

    db: Session = SessionLocal()
    try:
        # Row lock for the duration of the worker's DB write. Without it,
        # ``materialize_schedule_task.apply_schedule`` (an independent
        # Celery task) could SELECT the same row at the same version_id,
        # mutate it, and win the OCC race on flush — silently losing the
        # worker's pending ``is_deleted=True`` / ``status=cancelled``
        # write to a StaleDataError that ``record_audit`` swallows. With
        # the row lock the materializer's SELECT blocks until this
        # transaction commits; when it resumes it sees the worker-written
        # status (and the new cancelled-guard in ``set_schedule_dates``
        # keeps it from being overwritten back to scheduled).
        order = db.scalars(select(Order).where(Order.id == order_id).with_for_update()).first()
        if order is None:
            logger.warning(
                "schedule.db_action.missing_order",
                order_id=str(order_id),
                kind=kind,
            )
            return

        # Capture before commit — attributes expire after Session.commit().
        _order_number = order.order_number
        _created_by = order.created_by
        _oid = order.id

        if accepted:
            _apply_db_action_accept(db, order, kind, db_action_raw, actor_id)
        else:
            _apply_db_action_reject(db, order, kind, db_action_raw, actor_id)

        db.commit()

        # Pipeline-lag sample: time from enqueue (stamped by
        # ``schedule_queue.enqueue_compound``) to this commit. Only
        # recorded on the success path so the metric reflects "worker
        # kept up with the queue", not "worker failed and stopped".
        # Best-effort; ``record_lag_sample`` swallows its own errors.
        enqueued_at_ms = compound.get("_enqueued_at_ms")
        if isinstance(enqueued_at_ms, (int, float)):
            now_ms = int(time.time() * 1000)
            lag_ms = max(0, now_ms - int(enqueued_at_ms))
            record_lag_sample(lag_ms)

        # Send cancellation notification after commit (best-effort).
        # Triggers for: explicit user delete accepted, or orphan create
        # rejected (which includes "user cancelled a queued create compound"
        # — same DB end-state as "create compound was rejected by worker").
        _is_cancel = (
            (kind == "delete" and accepted)
            or (kind == "cancel" and accepted)
            or (kind == "create" and not accepted)
        )
        if _is_cancel:
            try:
                notification_service.create_notification(
                    db,
                    user_id=_created_by,
                    order_id=_oid,
                    type="order_cancelled",
                    message=f"訂單 {_order_number} 已被取消",
                )
            except Exception:
                logger.warning(
                    "notification.create_failed",
                    order_id=str(_oid),
                    user_id=str(_created_by),
                    exc_info=True,
                )
        _notify_rejected_pin_update(
            db,
            accepted=accepted,
            kind=kind,
            order_id=_oid,
            order_number=_order_number,
            created_by=_created_by,
            db_action_raw=db_action_raw,
        )
    finally:
        db.close()


def _notify_rejected_pin_update(
    db: Session,
    *,
    accepted: bool,
    kind: str,
    order_id: uuid.UUID,
    order_number: str,
    created_by: uuid.UUID,
    db_action_raw: dict[str, Any],
) -> None:
    """Notify the order creator that a rejected compound left a pin change unapplied.

    No-op unless this is a rejected *update* that targeted the pinned production
    date. Best-effort: notification failures are logged, never raised, so they
    cannot break the already-committed decision.
    """
    is_rejected_pin_update = (
        kind == "update"
        and not accepted
        and bool(db_action_raw.get("new_pinned_production_date_set"))
    )
    if not is_rejected_pin_update:
        return
    try:
        target_date = db_action_raw.get("new_pinned_production_date")
        detail = f"目標日期 {target_date}" if target_date else "解除固定"
        notification_service.create_notification(
            db,
            user_id=created_by,
            order_id=order_id,
            type="order_schedule_rejected",
            message=f"訂單 {order_number} 排程調整失敗, {detail} 未套用.",
        )
    except Exception:
        logger.warning(
            "notification.create_failed",
            order_id=str(order_id),
            user_id=str(created_by),
            exc_info=True,
        )


def _accept_create(order: Order) -> None:
    order.is_processing_locked = False


def _accept_update(
    db: Session,
    order: Order,
    db_action: dict[str, Any],
    actor_id: uuid.UUID | None,
) -> None:
    old_value: dict[str, Any] = {
        "wafer_quantity": order.wafer_quantity,
        "requested_delivery_date": str(order.requested_delivery_date),
        "notes": order.notes,
        "assigned_to": str(order.assigned_to) if order.assigned_to is not None else None,
        "is_pinned": order.is_pinned,
        "pinned_production_date": (
            str(order.pinned_production_date) if order.pinned_production_date is not None else None
        ),
    }
    new_qty = db_action.get("new_wafer_quantity")
    if new_qty is not None:
        order.wafer_quantity = int(new_qty)
    new_dl = db_action.get("new_requested_delivery_date")
    if new_dl is not None:
        order.requested_delivery_date = date.fromisoformat(str(new_dl))
    if db_action.get("new_notes_set"):
        order.notes = db_action.get("new_notes")
    if db_action.get("new_assigned_to_set"):
        raw_assignee = db_action.get("new_assigned_to")
        order.assigned_to = uuid.UUID(raw_assignee) if raw_assignee else None
    if db_action.get("new_pinned_production_date_set"):
        raw_pin_day = db_action.get("new_pinned_production_date")
        if raw_pin_day is None:
            order.is_pinned = False
            order.pinned_production_date = None
        else:
            order.is_pinned = True
            order.pinned_production_date = date.fromisoformat(str(raw_pin_day))
    order.is_processing_locked = False
    new_value: dict[str, Any] = {
        "wafer_quantity": order.wafer_quantity,
        "requested_delivery_date": str(order.requested_delivery_date),
        "notes": order.notes,
        "assigned_to": str(order.assigned_to) if order.assigned_to is not None else None,
        "is_pinned": order.is_pinned,
        "pinned_production_date": (
            str(order.pinned_production_date) if order.pinned_production_date is not None else None
        ),
    }
    _worker_audit(
        db,
        action="order.updated",
        actor_id=actor_id,
        order_id=order.id,
        old_value=old_value,
        new_value=new_value,
    )


def _accept_delete(
    db: Session,
    order: Order,
    db_action: dict[str, Any],
    actor_id: uuid.UUID | None,
) -> None:
    old_value: dict[str, Any] = {
        "status": order.status.value,
        "is_deleted": False,
        "wafer_quantity": db_action.get("old_wafer_quantity"),
        "requested_delivery_date": db_action.get("old_requested_delivery_date"),
        "notes": db_action.get("old_notes"),
        "assigned_to": db_action.get("old_assigned_to"),
    }
    order.is_deleted = True
    order.status = OrderStatus.cancelled
    order.is_processing_locked = False
    _worker_audit(
        db,
        action="order.deleted",
        actor_id=actor_id,
        order_id=order.id,
        old_value=old_value,
    )


def _accept_cancel(
    db: Session,
    order: Order,
    db_action: dict[str, Any],
    actor_id: uuid.UUID | None,
) -> None:
    old_value: dict[str, Any] = {
        "status": order.status.value,
        "is_deleted": False,
        "wafer_quantity": db_action.get("old_wafer_quantity"),
        "requested_delivery_date": db_action.get("old_requested_delivery_date"),
        "notes": db_action.get("old_notes"),
        "assigned_to": db_action.get("old_assigned_to"),
    }
    order.status = OrderStatus.cancelled
    order.is_processing_locked = False
    _worker_audit(
        db,
        action="order.cancelled",
        actor_id=actor_id,
        order_id=order.id,
        old_value=old_value,
    )


def _apply_db_action_accept(
    db: Session,
    order: Order,
    kind: str,
    db_action: dict[str, Any],
    actor_id: uuid.UUID | None,
) -> None:
    """Write the success-path DB changes for an accepted compound.

    Audit logs are emitted here (not at producer time) so the audit
    timestamp reflects when the change was actually committed.
    """
    if kind == "create":
        _accept_create(order)
    elif kind == "update":
        _accept_update(db, order, db_action, actor_id)
    elif kind == "delete":
        _accept_delete(db, order, db_action, actor_id)
    elif kind == "cancel":
        _accept_cancel(db, order, db_action, actor_id)


def _restore_order_fields_on_reject(order: Order, db_action: dict[str, Any]) -> None:
    """Roll an order's mutable fields back to their pre-update snapshot.

    Used when a compound *update* is rejected: each ``old_*`` value captured in
    ``db_action`` is restored only if the corresponding field was actually part
    of the rejected change. Field-presence keys (``new_*_set``) gate the restore
    so untouched fields are left alone.
    """
    if "old_wafer_quantity" in db_action and db_action.get("old_wafer_quantity") is not None:
        order.wafer_quantity = int(db_action["old_wafer_quantity"])
    if (
        "old_requested_delivery_date" in db_action
        and db_action.get("old_requested_delivery_date") is not None
    ):
        order.requested_delivery_date = date.fromisoformat(
            str(db_action["old_requested_delivery_date"])
        )
    if db_action.get("new_notes_set"):
        order.notes = db_action.get("old_notes")
    if db_action.get("new_assigned_to_set"):
        raw_assignee = db_action.get("old_assigned_to")
        order.assigned_to = uuid.UUID(raw_assignee) if raw_assignee else None
    if db_action.get("new_pinned_production_date_set"):
        raw_old_pin_day = db_action.get("old_pinned_production_date")
        order.is_pinned = bool(db_action.get("old_is_pinned"))
        order.pinned_production_date = (
            date.fromisoformat(str(raw_old_pin_day)) if raw_old_pin_day else None
        )
    if db_action.get("old_status") and "old_scheduled_production_date" in db_action:
        raw_scheduled_date = db_action.get("old_scheduled_production_date")
        order.scheduled_production_date = (
            date.fromisoformat(str(raw_scheduled_date)) if raw_scheduled_date else None
        )
    if db_action.get("old_status") and "old_expected_delivery_date" in db_action:
        raw_expected_date = db_action.get("old_expected_delivery_date")
        order.expected_delivery_date = (
            date.fromisoformat(str(raw_expected_date)) if raw_expected_date else None
        )
    if db_action.get("old_status") and "old_daily_breakdown" in db_action:
        order.daily_breakdown = db_action.get("old_daily_breakdown")
    if db_action.get("old_status"):
        order.status = OrderStatus(str(db_action["old_status"]))


def _apply_db_action_reject(
    db: Session,
    order: Order,
    kind: str,
    db_action: dict[str, Any],
    actor_id: uuid.UUID | None,
) -> None:
    """Compensate for a rejected compound: clear lock; for create, orphan-cleanup.

    For update/delete the DB columns the user wanted to change were
    never written by the producer, so "rollback" reduces to clearing
    the lock and snapping ``status`` back to whatever the row's actual
    schedule presence implies (scheduled vs pending). For create the
    producer DID write a stub row, so we soft-delete it.

    Audit (PR review feedback): create-reject also writes an
    ``order.cancelled`` audit row so that user-initiated cancel-of-create
    leaves a trail — without it, the history shows ``order.created``
    followed by the row vanishing with no closing event, which is bad
    for compliance review and operational forensics. The cancel-endpoint
    failure path needs the same audit symmetry as the worker reject path.

    N-4 round-2 guard: never demote ``in_production`` here. Today the
    producer-side ``MUTABLE_STATUSES`` check already blocks PATCH /
    DELETE on an in-production row, so this branch is unreachable in
    practice. But if a future change opens up partial mutation (e.g.
    "you can change notes on a row that's mid-production"), the
    unconditional ``order.status = scheduled`` write below would
    silently demote the row mid-shift and break the same downstream
    invariant ``set_schedule_dates`` defends against. Cheaper to defend
    here than to forget when MUTABLE_STATUSES is relaxed.
    """
    if kind == "create":
        # Business rule: a create compound that the scheduler rejects (or
        # that a user retracts via cancel-of-create) leaves the row
        # **visible** to the user. ``status='cancelled'`` documents the
        # outcome but ``is_deleted`` stays False so:
        #
        # - The order still appears in ``GET /orders``, letting the user
        #   review what happened (e.g., "ORD-X was rejected:
        #   deadline_too_far") alongside the notification-center entry.
        # - Only the explicit ``DELETE /orders/{id}`` path sets
        #   ``is_deleted=True`` (that's an active user intent to remove
        #   the row from their workspace).
        #
        # Before this change every create-reject permanently soft-deleted
        # the row, hiding the failure from the user — they got the
        # "compound_failed" WS event but couldn't see the offending order
        # anymore. Two-axis split (status + is_deleted) makes the
        # distinction explicit.
        old_value: dict[str, Any] = {
            "status": order.status.value,
            "is_deleted": False,
            "wafer_quantity": order.wafer_quantity,
            "requested_delivery_date": str(order.requested_delivery_date),
        }
        order.status = OrderStatus.cancelled
        order.is_processing_locked = False
        # Intentionally NOT setting is_deleted=True — see comment above.
        _worker_audit(
            db,
            action="order.cancelled",
            actor_id=actor_id,
            order_id=order.id,
            old_value=old_value,
        )
        return

    # update / delete / cancel: just unlock and restore status. The
    # cancel reject path mirrors delete reject — both compounds were
    # ``[remove]`` / ``[unpin, remove]`` and neither touched any
    # user-facing column at producer time, so rollback is the same
    # "clear lock + recompute status from scheduled_production_date".
    order.is_processing_locked = False
    if order.status == OrderStatus.in_production:
        # Defensive — see docstring.
        return
    # Terminal-state guard: a row that's already ``cancelled`` or
    # soft-deleted must NOT have its status rewritten back to scheduled
    # / pending. This is reachable when a producer enqueues a compound
    # against a row whose state has since terminalized (e.g., a
    # ``kind="delete"`` compound built against a row that was, by the
    # time the worker rejected the compound, already cancelled by an
    # earlier-accepted cancel). Without this guard, the reject's
    # "recompute status from ``scheduled_production_date``" branch
    # would silently un-cancel the row — a worse outcome than the
    # compound that failed.
    if order.status == OrderStatus.cancelled or order.is_deleted:
        return
    if kind == "update":
        _restore_order_fields_on_reject(order, db_action)
    if db_action.get("old_status"):
        return
    if order.scheduled_production_date is not None:
        order.status = OrderStatus.scheduled
    else:
        order.status = OrderStatus.pending


def _worker_audit(
    db: Session,
    *,
    action: str,
    actor_id: uuid.UUID | None,
    order_id: uuid.UUID,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    """Write an audit row + ECS stdout record from inside the deferred-finalize path.

    Thin wrapper around :func:`app.core.audit.record_audit` that keeps the
    ``actor_id`` / ``order_id`` signature contract — the worker / cancel
    paths don't load the ``User`` row, the id rides in the compound's
    ``db_action`` payload, so we take a raw UUID instead of the
    ``actor: User`` shape used by ``services/order._write_audit``.
    """
    record_audit(
        db,
        action=action,
        actor_id=actor_id,
        resource_type="order",
        resource_id=order_id,
        old_value=old_value,
        new_value=new_value,
    )
