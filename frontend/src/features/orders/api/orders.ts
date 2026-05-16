/**
 * Orders API client — Zod schemas + React Query hooks.
 * All fetch calls are wrapped here; components never call fetch directly.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';

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

const auditLogEntrySchema = z.object({
  id: z.string().uuid(),
  action: z.string(),
  user_id: z.string().uuid().nullable(),
  resource_id: z.string().uuid(),
  old_value: z.record(z.unknown()).nullable(),
  new_value: z.record(z.unknown()).nullable(),
  created_at: z.string(),
});

const scheduleTriggerResponseSchema = z.object({
  task_id: z.string(),
  message: z.string(),
});

// ---------------------------------------------------------------------------
// Shared fetch helper
// ---------------------------------------------------------------------------

function jsonHeaders(): HeadersInit {
  return { 'Content-Type': 'application/json' };
}

async function apiFetch<T>(url: string, init: RequestInit, parse: (raw: unknown) => T): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any, @typescript-eslint/no-unsafe-assignment
    const body = await res.json().catch((): any => ({}));
    // eslint-disable-next-line @typescript-eslint/no-unsafe-member-access
    const errorMessage = body?.error?.message as string | undefined;
    // eslint-disable-next-line @typescript-eslint/no-unsafe-member-access
    const detail = body?.detail as string | undefined;
    const msg: string = errorMessage ?? detail ?? res.statusText;
    throw new Error(msg);
  }
  if (res.status === 204) return undefined as T;
  return parse(await res.json());
}

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
  assignedTo?: string[];
  createdBy?: string[];
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
      void qc.invalidateQueries({ queryKey: orderKeys.all });
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
      void qc.invalidateQueries({ queryKey: orderKeys.all });
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
      void qc.invalidateQueries({ queryKey: orderKeys.all });
    },
  });
}

/**
 * Manually kick the scheduler.
 *
 * Backend already enqueues compounds automatically on every order CRUD
 * (`POST /orders`, `PATCH /orders/{id}`, `DELETE /orders/{id}`), so the
 * frontend should NOT build its own compound payload — doing so would
 * double-enqueue the order and skip the backend's lock / audit / rollback
 * machinery. This endpoint just tells the scheduler to drain its pending
 * queue now. No order payload.
 *
 * If a per-order "reschedule this one" semantic is needed in the future,
 * ask the backend to add a dedicated endpoint
 * (e.g. POST /orders/{id}/reschedule) rather than driving the compound API
 * from the frontend.
 */
export function useTriggerSchedule(): ReturnType<typeof useMutation<ScheduleTriggerResponse>> {
  return useMutation<ScheduleTriggerResponse>({
    mutationFn: () =>
      apiFetch('/api/v1/schedule/trigger', { method: 'POST', credentials: 'include' }, (d) =>
        scheduleTriggerResponseSchema.parse(d),
      ),
  });
}

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
      void qc.invalidateQueries({ queryKey: orderKeys.all });
    },
  });
}

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
