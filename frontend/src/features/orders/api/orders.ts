/**
 * Orders API client — Zod schemas + React Query hooks.
 * All fetch calls are wrapped here; components never call fetch directly.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';

import { apiFetch, jsonHeaders } from '@/lib/apiFetch';
import { useCurrentUser } from '@/lib/auth';

import type {
  AuditLogEntry,
  BatchUpdateRequest,
  BatchUpdateResponse,
  Order,
  OrderCreate,
  OrderListResponse,
  OrderUpdate,
  ScheduleTriggerResponse,
} from '../types';

import { scheduleCapacityKeys } from './scheduleCapacity';

// ---------------------------------------------------------------------------
// Zod schemas (runtime validation of API responses)
// ---------------------------------------------------------------------------

const orderStatusSchema = z.enum([
  'pending',
  'scheduled',
  'in_production',
  'completed',
  'cancelled',
]);

const orderSchema = z.object({
  id: z.string().uuid(),
  order_number: z.string(),
  customer_name: z.string(),
  wafer_quantity: z.number().int(),
  requested_delivery_date: z.string(),
  scheduled_production_date: z.string().nullable(),
  expected_delivery_date: z.string().nullable(),
  status: orderStatusSchema,
  assigned_to: z.string().nullable(),
  created_by: z.string().uuid(),
  notes: z.string().nullable(),
  pinned_production_date: z.string().nullable(),
  is_pinned: z.boolean(),
  is_processing_locked: z.boolean(),
  version_id: z.number().int(),
  created_at: z.string(),
  updated_at: z.string(),
});

const orderListSchema = z.object({
  items: z.array(orderSchema),
  total: z.number().int(),
  page: z.number().int(),
  page_size: z.number().int(),
});

const batchUpdateResponseSchema = z.object({
  updated_count: z.number().int(),
  skipped_count: z.number().int(),
  skipped_ids: z.array(z.string().uuid()),
});

const scheduleTriggerResponseSchema = z.object({
  task_id: z.string(),
  message: z.string(),
});

const auditLogEntrySchema = z.object({
  id: z.string().uuid(),
  action: z.string(),
  user_id: z.string().uuid().nullable(),
  resource_id: z.string().uuid(),
  old_value: z.record(z.unknown()).nullable(),
  new_value: z.record(z.unknown()).nullable(),
  created_at: z.string(),
});

// ---------------------------------------------------------------------------
// Query key factory
// ---------------------------------------------------------------------------

export const orderKeys = {
  all: ['orders'] as const,
  list: (params: object) => ['orders', 'list', params] as const,
};

// ---------------------------------------------------------------------------
// React Query hooks
// ---------------------------------------------------------------------------

export interface ListOrdersParams {
  status?: string | null;
  search?: string | null;
  assignedTo?: string[] | undefined;
  createdBy?: string[] | undefined;
  page?: number;
  page_size?: number;
  sortBy?: string;
  sortOrder?: 'asc' | 'desc';
}

export function useOrders(
  params: ListOrdersParams,
): ReturnType<typeof useQuery<OrderListResponse>> {
  const user = useCurrentUser();

  const qs = new URLSearchParams();
  if (params.status) qs.set('status', params.status);
  if (params.search) qs.set('search', params.search);
  params.assignedTo?.forEach((id) => {
    qs.append('assigned_to', id);
  });
  params.createdBy?.forEach((id) => {
    qs.append('created_by', id);
  });
  if (params.page != null) qs.set('page', String(params.page));
  if (params.page_size != null) qs.set('page_size', String(params.page_size));
  if (params.sortBy) qs.set('sort_by', params.sortBy);
  if (params.sortOrder) qs.set('sort_order', params.sortOrder);

  return useQuery<OrderListResponse>({
    queryKey: orderKeys.list(params),
    queryFn: () =>
      apiFetch(`/api/v1/orders?${qs.toString()}`, { credentials: 'include' }, (d) =>
        orderListSchema.parse(d),
      ),
    enabled: Boolean(user),
    refetchInterval: (query) => {
      const hasLocked = query.state.data?.items.some((o) => o.is_processing_locked);
      return hasLocked ? 3000 : false;
    },
  });
}

/**
 * All four mutations below invalidate ``scheduleCapacityKeys.all`` in addition
 * to ``orderKeys.all``. Creating, editing, deleting, or cancelling an order
 * eventually shifts the scheduler's per-day used/remaining via a worker
 * compound; ``useScheduleWs`` will refresh capacity once the worker emits
 * ``schedule.*``, but the eager local invalidate closes the window between
 * the API ack and the worker broadcast — important for any UI surface that
 * reads capacity (e.g. ``OrdersCalendarDialog``).
 */
export function useCreateOrder(): ReturnType<typeof useMutation<Order, Error, OrderCreate>> {
  const qc = useQueryClient();

  return useMutation<Order, Error, OrderCreate>({
    mutationFn: (payload) =>
      apiFetch(
        '/api/v1/orders',
        {
          method: 'POST',
          credentials: 'include',
          headers: jsonHeaders(),
          body: JSON.stringify(payload),
        },
        (d) => orderSchema.parse(d),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: orderKeys.all }).catch(() => {});
      qc.invalidateQueries({ queryKey: scheduleCapacityKeys.all }).catch(() => {});
    },
  });
}

export function useUpdateOrder(): ReturnType<
  typeof useMutation<Order, Error, { id: string; payload: OrderUpdate }>
> {
  const qc = useQueryClient();

  return useMutation<Order, Error, { id: string; payload: OrderUpdate }>({
    mutationFn: ({ id, payload }) =>
      apiFetch(
        `/api/v1/orders/${id}`,
        {
          method: 'PATCH',
          credentials: 'include',
          headers: jsonHeaders(),
          body: JSON.stringify(payload),
        },
        (d) => orderSchema.parse(d),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: orderKeys.all }).catch(() => {});
      qc.invalidateQueries({ queryKey: scheduleCapacityKeys.all }).catch(() => {});
    },
  });
}

export function useDeleteOrder(): ReturnType<typeof useMutation<undefined, Error, string>> {
  const qc = useQueryClient();

  return useMutation<undefined, Error, string>({
    mutationFn: (id) =>
      apiFetch<undefined>(
        `/api/v1/orders/${id}`,
        { method: 'DELETE', credentials: 'include' },
        () => undefined,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: orderKeys.all }).catch(() => {});
      qc.invalidateQueries({ queryKey: scheduleCapacityKeys.all }).catch(() => {});
    },
  });
}

export function useCancelOrder(): ReturnType<typeof useMutation<Order, Error, string>> {
  const qc = useQueryClient();

  return useMutation<Order, Error, string>({
    mutationFn: (id) =>
      apiFetch(`/api/v1/orders/${id}/cancel`, { method: 'POST', credentials: 'include' }, (d) =>
        orderSchema.parse(d),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: orderKeys.all }).catch(() => {});
      qc.invalidateQueries({ queryKey: scheduleCapacityKeys.all }).catch(() => {});
    },
  });
}

/**
 * Kick the global scheduler to drain its pending queue now.
 * The backend enqueues compounds automatically on order CRUD, so the
 * frontend must NOT build compound payloads — just call this trigger.
 */
export function useTriggerSchedule(): ReturnType<typeof useMutation<ScheduleTriggerResponse>> {
  return useMutation<ScheduleTriggerResponse>({
    mutationFn: () =>
      apiFetch('/api/v1/schedule/trigger', { method: 'POST', credentials: 'include' }, (d) =>
        scheduleTriggerResponseSchema.parse(d),
      ),
  });
}

/**
 * TODO(future PR): wire into a multi-select bulk-update toolbar on the
 * Orders table (SUMMARY-zh feature #7 — "useBatchUpdateOrders 接 UI").
 * Intended consumer: a "Bulk reassign / status change" action surfaced
 * once row selection ships in the orders feature (rough scope: add a
 * selection column + a toolbar that calls this hook with the selected
 * IDs — ~80 LoC of new UI). Kept here because the backend endpoint and
 * Zod contract are already stable; only the UI surface is missing, and
 * adding it now is out of scope for the convention-cleanup PR.
 */
export function useBatchUpdateOrders(): ReturnType<
  typeof useMutation<BatchUpdateResponse, Error, BatchUpdateRequest>
> {
  const qc = useQueryClient();

  return useMutation<BatchUpdateResponse, Error, BatchUpdateRequest>({
    mutationFn: (payload) =>
      apiFetch(
        '/api/v1/orders/batch-update',
        {
          method: 'PATCH',
          credentials: 'include',
          headers: jsonHeaders(),
          body: JSON.stringify(payload),
        },
        (d) => batchUpdateResponseSchema.parse(d),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: orderKeys.all }).catch(() => {});
    },
  });
}

/**
 * TODO(future PR): wire into an order-history drawer / dialog on
 * `OrderTable` (and eventually `OrderDetail`). Intended consumer:
 * a chronological audit-trail view that renders each entry with the
 * action, the actor's username (resolved via `useUsernames`), the
 * timestamp, and a diff of `old_value` / `new_value` — rough scope
 * ~60 LoC of new UI. Kept here because `/orders/{id}/audit-log` is
 * already implemented backend-side; the missing piece is the dialog
 * surface, which is out of scope for the convention-cleanup PR.
 */
export function useOrderAuditLog(orderId: string): ReturnType<typeof useQuery<AuditLogEntry[]>> {
  return useQuery<AuditLogEntry[]>({
    queryKey: ['orders', 'audit-log', orderId],
    queryFn: () =>
      apiFetch(`/api/v1/orders/${orderId}/audit-log`, { credentials: 'include' }, (d) =>
        z.array(auditLogEntrySchema).parse(d),
      ),
    enabled: Boolean(orderId),
  });
}
