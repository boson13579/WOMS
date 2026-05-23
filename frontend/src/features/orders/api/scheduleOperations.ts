/**
 * Pin / unpin orders by going through the same row-locked PATCH /orders/{id}
 * endpoint that handles qty / deadline / notes changes.
 *
 * Pre-migration this hook POSTed a raw compound to /api/v1/schedule/operations
 * — that endpoint had no row-level lock or db_action protection, so it could
 * race against concurrent PATCHes on the same order and leak stale compounds
 * past the segment-tree invariant guard. The backend now folds pin into the
 * regular update flow: PATCH body ``{pinned_production_date: <day>}`` pins,
 * ``{pinned_production_date: null}`` unpins, and the existing ``SELECT FOR
 * UPDATE`` row lock + auto-generated db_action protect the order against
 * concurrent producer races.
 *
 * Multi-order pin (UI lets the user drag several orders to dates at once)
 * fans out to N parallel PATCHes — each gets its own per-order row lock
 * and its own success/failure response. The mutation surfaces either:
 *
 * - ``success`` — every PATCH returned 2xx (compound enqueued for each)
 * - ``error``  — at least one PATCH failed; the error message names the
 *   first failing order and the reason
 *
 * Follow-up confirmation that the pin landed in the segment tree still
 * comes via WebSocket (``schedule.materialized``) + re-fetching scheduled
 * orders — the caller (``OrdersCalendarDialog``) already wires that up.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';

import { apiFetch, jsonHeaders } from '@/lib/apiFetch';
import { useCurrentUserId } from '@/lib/auth';

import type { Order } from '../types';

import { orderKeys } from './orders';
import { scheduleCapacityKeys } from './scheduleCapacity';
import { scheduleResultKeys } from './scheduleResult';

export interface ScheduleCompoundResponse {
  // Backend's PATCH endpoint returns the updated OrderResponse. We surface a
  // compound-shaped object for backward-compat with callers that key off
  // some opaque identifier; the synthetic id is the user-supplied compound_id
  // (from the calendar dialog) — not the per-order compound ids the backend
  // generates.
  compound_id: string;
  message: string;
}

type SchedulableOrder = Pick<
  Order,
  | 'id'
  | 'order_number'
  | 'wafer_quantity'
  | 'requested_delivery_date'
  | 'status'
  | 'is_pinned'
  | 'pinned_production_date'
  | 'version_id'
>;

export interface PinScheduleInput {
  /** Caller-generated id — used for UI tracking only; backend ignores. */
  compoundId: string;
  targets: {
    order: SchedulableOrder;
    /** ``null`` = unpin; date string = pin to that day. */
    targetDate: string | null;
  }[];
}

interface PatchOrderResponse {
  id: string;
  version_id: number;
}

async function patchPinForOrder(
  order: SchedulableOrder,
  targetDate: string | null,
): Promise<PatchOrderResponse> {
  return apiFetch(
    `/api/v1/orders/${order.id}`,
    {
      method: 'PATCH',
      credentials: 'include',
      headers: jsonHeaders(),
      body: JSON.stringify({
        pinned_production_date: targetDate, // explicit null = unpin
        version_id: order.version_id,
      }),
    },
    // Lenient parse — we just need the id back to know the PATCH landed.
    // Full OrderResponse has many more fields; we don't need them here.
    (d): PatchOrderResponse => {
      const obj = d as { id?: string; version_id?: number };
      if (typeof obj.id !== 'string' || typeof obj.version_id !== 'number') {
        throw new Error('Malformed PATCH response from /orders/{id}');
      }
      return { id: obj.id, version_id: obj.version_id };
    },
  );
}

export function usePinScheduleOperation(): ReturnType<
  typeof useMutation<ScheduleCompoundResponse, Error, PinScheduleInput>
> {
  const qc = useQueryClient();
  const userId = useCurrentUserId();

  return useMutation<ScheduleCompoundResponse, Error, PinScheduleInput>({
    mutationFn: async (input) => {
      if (!userId) {
        throw new Error('You must be logged in to schedule orders.');
      }
      // Fire all PATCHes in parallel. ``Promise.all`` rejects on the FIRST
      // failure; for multi-target flows we'd want ``allSettled`` + aggregate
      // error, but the calendar dialog currently treats partial failure
      // identically to total failure (any failed pin clears the whole
      // pending-moves batch), so all-or-nothing is fine here.
      await Promise.all(
        input.targets.map(({ order, targetDate }) => patchPinForOrder(order, targetDate)),
      );
      return {
        compound_id: input.compoundId,
        message: 'PATCH issued for each target order; awaiting worker materialization.',
      };
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: orderKeys.all });
      void qc.invalidateQueries({ queryKey: scheduleCapacityKeys.all });
      void qc.invalidateQueries({ queryKey: scheduleResultKeys.all });
    },
  });
}
