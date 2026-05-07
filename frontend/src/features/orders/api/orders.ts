/**
 * Orders API client — Zod schemas + React Query hooks.
 * All fetch calls are wrapped here; components never call fetch directly.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';

import { useAuthStore } from '@/features/auth/stores/authStore';

import { MOCK_ORDER_LIST } from './mockData';

import type {
  Order,
  OrderCreate,
  OrderListResponse,
  OrderUpdate,
  ScheduleTaskResponse,
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

const scheduleTaskSchema = z.object({
  task_id: z.string(),
  order_id: z.string(),
  message: z.string(),
});

export const scheduleProgressSchema = z.object({
  task_id: z.string(),
  order_id: z.string(),
  status: z.enum(['started', 'analyzing', 'optimizing', 'applying', 'completed']),
  progress: z.number().int().min(0).max(100),
  message: z.string(),
});

// ---------------------------------------------------------------------------
// Shared fetch helper
// ---------------------------------------------------------------------------

function bearerHeaders(token: string): HeadersInit {
  return { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };
}

async function apiFetch<T>(
  url: string,
  init: RequestInit,
  parse: (raw: unknown) => T,
): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const body = await res.json().catch((): any => ({}));
    const msg: string =
      // eslint-disable-next-line @typescript-eslint/no-unsafe-member-access
      (body?.error?.message as string | undefined) ?? res.statusText;
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
  page?: number;
  page_size?: number;
  // swap these for qs params once the backend supports sort_by / sort_order
  sortBy?: string;
  sortOrder?: 'asc' | 'desc';
}

export function useOrders(params: ListOrdersParams): ReturnType<
  typeof useQuery<OrderListResponse>
> {
  const token = useAuthStore((s) => s.token) ?? 'mytoken';

  const qs = new URLSearchParams();
  if (params.status) qs.set('status', params.status);
  // backend does not support free-text search yet; omit until the endpoint is ready
  if (params.page != null) qs.set('page', String(params.page));
  if (params.page_size != null) qs.set('page_size', String(params.page_size));

  return useQuery<OrderListResponse>({
    queryKey: orderKeys.list(params),
    queryFn: async () => {
      const { sortBy = 'order_number', sortOrder = 'asc' } = params;
      const dir = sortOrder === 'desc' ? -1 : 1;

      function sortItems(items: Order[]): Order[] {
        return [...items].sort((a, b) => {
          const av = a[sortBy as keyof Order];
          const bv = b[sortBy as keyof Order];
          if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * dir;
          return String(av ?? '').localeCompare(String(bv ?? '')) * dir;
        });
      }

      function filterBySearch(items: Order[]): Order[] {
        if (!params.search) return items;
        const q = params.search.toLowerCase();
        return items.filter(
          (o) =>
            o.order_number.toLowerCase().includes(q) ||
            o.customer_name.toLowerCase().includes(q),
        );
      }

      try {
        const result = await apiFetch(
          `/api/v1/orders?${qs.toString()}`,
          { headers: bearerHeaders(token) },
          (d) => orderListSchema.parse(d),
        );
        // client-side search + sort on current page — backend does not support these params yet
        const filtered = filterBySearch(result.items);
        return { ...result, items: sortItems(filtered) };
      } catch (err) {
        if (!import.meta.env.DEV) throw err;
        // DEV only: API unreachable or token invalid — filter mock data client-side
        let items = MOCK_ORDER_LIST.items;
        if (params.status) {
          items = items.filter((o) => o.status === params.status);
        }
        items = sortItems(filterBySearch(items)); // sort before pagination for correct results
        const page = params.page ?? 1;
        const size = params.page_size ?? 20;
        return {
          items: items.slice((page - 1) * size, page * size),
          total: items.length,
          page,
          page_size: size,
        };
      }
    },
    enabled: Boolean(token),
  });
}

export function useCreateOrder(): ReturnType<typeof useMutation<Order, Error, OrderCreate>> {
  const token = useAuthStore((s) => s.token) ?? 'mytoken';
  const qc = useQueryClient();

  return useMutation<Order, Error, OrderCreate>({
    mutationFn: (payload) =>
      apiFetch(
        '/api/v1/orders',
        { method: 'POST', headers: bearerHeaders(token), body: JSON.stringify(payload) },
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
  const token = useAuthStore((s) => s.token) ?? 'mytoken';
  const qc = useQueryClient();

  return useMutation<Order, Error, { id: string; payload: OrderUpdate }>({
    mutationFn: ({ id, payload }) =>
      apiFetch(
        `/api/v1/orders/${id}`,
        { method: 'PATCH', headers: bearerHeaders(token), body: JSON.stringify(payload) },
        (d) => orderSchema.parse(d),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: orderKeys.all });
    },
  });
}

export function useDeleteOrder(): ReturnType<typeof useMutation<undefined, Error, string>> {
  const token = useAuthStore((s) => s.token) ?? 'mytoken';
  const qc = useQueryClient();

  return useMutation<undefined, Error, string>({
    mutationFn: (id) =>
      apiFetch<undefined>(
        `/api/v1/orders/${id}`,
        { method: 'DELETE', headers: bearerHeaders(token) },
        () => undefined,
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: orderKeys.all });
    },
  });
}

export function useTriggerSchedule(): ReturnType<
  typeof useMutation<ScheduleTaskResponse, Error, string>
> {
  const token = useAuthStore((s) => s.token) ?? 'mytoken';

  return useMutation<ScheduleTaskResponse, Error, string>({
    mutationFn: (orderId) =>
      apiFetch(
        `/api/v1/orders/${orderId}/schedule`,
        { method: 'POST', headers: bearerHeaders(token) },
        (d) => scheduleTaskSchema.parse(d),
      ),
  });
}