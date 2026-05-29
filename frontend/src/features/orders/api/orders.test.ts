/**
 * Coverage for orders.ts — query-string building, mutation payloads,
 * cache invalidation, error paths, and conditional refetch.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { act, type ReactNode } from 'react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  orderKeys,
  useBatchUpdateOrders,
  useCancelOrder,
  useCreateOrder,
  useDeleteOrder,
  useOrderAuditLog,
  useOrders,
  useTriggerSchedule,
  useUpdateOrder,
} from './orders';

// ---------------------------------------------------------------------------
// Mock @/lib/auth
// ---------------------------------------------------------------------------

const mockAuth = {
  user: { id: 'user-uuid-001', username: 'alice', role: 'scheduler' } as {
    id: string;
    username: string;
    role: string;
  } | null,
};

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => mockAuth.user,
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let qc: QueryClient;

function makeWrapper() {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  }
  return Wrapper;
}

const orderFixture = {
  id: '11111111-1111-4111-8111-111111111111',
  order_number: 'ORD-20260504-0001',
  customer_name: 'Acme Corp',
  wafer_quantity: 500,
  requested_delivery_date: '2026-06-01',
  scheduled_production_date: null,
  expected_delivery_date: null,
  status: 'pending' as const,
  assigned_to: null,
  created_by: '22222222-2222-4222-8222-222222222222',
  notes: null,
  pinned_production_date: null,
  is_pinned: false,
  is_processing_locked: false,
  version_id: 1,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  cleanup();
  // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- qc may not be set when tests don't render hooks via makeWrapper().
  qc?.clear();
  vi.clearAllMocks();
});

beforeEach(() => {
  mockAuth.user = { id: 'user-uuid-001', username: 'alice', role: 'scheduler' };
});

// ---------------------------------------------------------------------------
// orderKeys factory
// ---------------------------------------------------------------------------

describe('orderKeys', () => {
  it('exposes a stable "all" tuple', () => {
    // Required so other code can pass it as queryKey for invalidation.
    expect(orderKeys.all).toEqual(['orders']);
  });

  it('builds list keys with the params object embedded', () => {
    const params = { page: 3, status: 'pending' };
    expect(orderKeys.list(params)).toEqual(['orders', 'list', params]);
  });
});

// ---------------------------------------------------------------------------
// useOrders — query-string assembly + enabled gating + refetchInterval
// ---------------------------------------------------------------------------

describe('useOrders query-string assembly', () => {
  beforeEach(() => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ items: [], total: 0, page: 1, page_size: 20 }),
    );
  });

  it('omits all optional query params when nothing is passed', async () => {
    const { result } = renderHook(() => useOrders({}), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const url = String(vi.mocked(global.fetch).mock.calls[0][0]);
    // Trailing "?" with no params is expected because URLSearchParams toString is empty.
    expect(url).toBe('/api/v1/orders?');
  });

  it('appends every supported param when fully populated', async () => {
    const { result } = renderHook(
      () =>
        useOrders({
          status: 'pending',
          search: 'acme',
          assignedTo: ['u-1', 'u-2'],
          createdBy: ['u-3'],
          page: 2,
          page_size: 50,
          sortBy: 'created_at',
          sortOrder: 'desc',
        }),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const url = String(vi.mocked(global.fetch).mock.calls[0][0]);
    expect(url).toContain('status=pending');
    expect(url).toContain('search=acme');
    expect(url).toContain('assigned_to=u-1');
    expect(url).toContain('assigned_to=u-2');
    expect(url).toContain('created_by=u-3');
    expect(url).toContain('page=2');
    expect(url).toContain('page_size=50');
    expect(url).toContain('sort_by=created_at');
    expect(url).toContain('sort_order=desc');
  });

  it('skips null/undefined optional filters (falsy short-circuit)', async () => {
    const { result } = renderHook(
      () =>
        useOrders({
          status: null,
          search: null,
          assignedTo: undefined,
          createdBy: undefined,
        }),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const url = String(vi.mocked(global.fetch).mock.calls[0][0]);
    expect(url).not.toContain('status=');
    expect(url).not.toContain('search=');
    expect(url).not.toContain('assigned_to=');
    expect(url).not.toContain('created_by=');
  });

  it('still emits page=0 (page != null) instead of treating it as falsy', async () => {
    const { result } = renderHook(() => useOrders({ page: 0, page_size: 0 }), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const url = String(vi.mocked(global.fetch).mock.calls[0][0]);
    expect(url).toContain('page=0');
    expect(url).toContain('page_size=0');
  });

  it('is disabled when no user is logged in', () => {
    mockAuth.user = null;
    const { result } = renderHook(() => useOrders({}), { wrapper: makeWrapper() });

    expect(result.current.fetchStatus).toBe('idle');
    expect(vi.mocked(global.fetch)).not.toHaveBeenCalled();
  });

  it('surfaces 500 errors from the backend', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ error: { code: 500, message: 'Boom' } }, 500),
    );

    const { result } = renderHook(() => useOrders({}), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.data).toBeUndefined();
  });

  it('refetchInterval returns 3000 when at least one order is processing-locked', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({
        items: [{ ...orderFixture, is_processing_locked: true }],
        total: 1,
        page: 1,
        page_size: 20,
      }),
    );

    const { result } = renderHook(() => useOrders({}), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    // Pull the live query observer to inspect the resolved refetchInterval.
    const cached = qc.getQueryCache().findAll({ queryKey: orderKeys.all });
    expect(cached.length).toBeGreaterThan(0);
    const query = cached[0];
    const opts = query.options as { refetchInterval?: (q: typeof query) => number | false };
    expect(typeof opts.refetchInterval).toBe('function');
    const interval = opts.refetchInterval?.(query);
    expect(interval).toBe(3000);
  });

  it('refetchInterval returns false when no orders are locked', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ items: [orderFixture], total: 1, page: 1, page_size: 20 }),
    );

    const { result } = renderHook(() => useOrders({}), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const cached = qc.getQueryCache().findAll({ queryKey: orderKeys.all });
    const query = cached[0];
    const opts = query.options as { refetchInterval?: (q: typeof query) => number | false };
    expect(opts.refetchInterval?.(query)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// useCreateOrder
// ---------------------------------------------------------------------------

describe('useCreateOrder', () => {
  it('POSTs to /orders with JSON body and invalidates both caches on success', async () => {
    vi.mocked(global.fetch).mockResolvedValue(jsonResponse(orderFixture, 201));

    const { result } = renderHook(() => useCreateOrder(), { wrapper: makeWrapper() });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    const payload = {
      customer_name: 'Acme Corp',
      wafer_quantity: 500,
      requested_delivery_date: '2026-06-01',
    };
    act(() => {
      result.current.mutate(payload as never);
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [url, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(url).toBe('/api/v1/orders');
    expect(init).toMatchObject({ method: 'POST', credentials: 'include' });
    expect((init?.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(JSON.parse(init?.body as string)).toEqual(payload);

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['orders'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['schedule', 'capacity-usage'] });
  });

  it('rejects on 400 validation errors', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ error: { code: 400, message: 'invalid wafer_quantity' } }, 400),
    );

    const { result } = renderHook(() => useCreateOrder(), { wrapper: makeWrapper() });

    act(() => {
      result.current.mutate({} as never);
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toContain('invalid wafer_quantity');
  });
});

// ---------------------------------------------------------------------------
// useUpdateOrder
// ---------------------------------------------------------------------------

describe('useUpdateOrder', () => {
  it('PATCHes /orders/{id} with the supplied payload and invalidates caches', async () => {
    vi.mocked(global.fetch).mockResolvedValue(jsonResponse(orderFixture));

    const { result } = renderHook(() => useUpdateOrder(), { wrapper: makeWrapper() });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    act(() => {
      result.current.mutate({
        id: orderFixture.id,
        payload: { wafer_quantity: 600, version_id: 1 } as never,
      });
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [url, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(url).toBe(`/api/v1/orders/${orderFixture.id}`);
    expect(init).toMatchObject({ method: 'PATCH', credentials: 'include' });
    expect(JSON.parse(init?.body as string)).toEqual({ wafer_quantity: 600, version_id: 1 });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['orders'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['schedule', 'capacity-usage'] });
  });

  it('surfaces 409 concurrent-modification errors', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ error: { code: 409, message: 'modified by another user' } }, 409),
    );

    const { result } = renderHook(() => useUpdateOrder(), { wrapper: makeWrapper() });

    act(() => {
      result.current.mutate({ id: orderFixture.id, payload: {} as never });
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toContain('modified by another user');
  });
});

// ---------------------------------------------------------------------------
// useDeleteOrder
// ---------------------------------------------------------------------------

describe('useDeleteOrder', () => {
  it('issues DELETE /orders/{id} and resolves with undefined on 204', async () => {
    vi.mocked(global.fetch).mockResolvedValue(new Response(null, { status: 204 }));

    const { result } = renderHook(() => useDeleteOrder(), { wrapper: makeWrapper() });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    act(() => {
      result.current.mutate(orderFixture.id);
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [url, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(url).toBe(`/api/v1/orders/${orderFixture.id}`);
    expect(init).toMatchObject({ method: 'DELETE', credentials: 'include' });
    expect(result.current.data).toBeUndefined();
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['orders'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['schedule', 'capacity-usage'] });
  });

  it('rejects on 404 not-found', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ error: { code: 404, message: 'Order not found' } }, 404),
    );

    const { result } = renderHook(() => useDeleteOrder(), { wrapper: makeWrapper() });

    act(() => {
      result.current.mutate(orderFixture.id);
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toContain('not found');
  });
});

// ---------------------------------------------------------------------------
// useCancelOrder
// ---------------------------------------------------------------------------

describe('useCancelOrder', () => {
  it('POSTs to /orders/{id}/cancel and invalidates caches', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ ...orderFixture, status: 'cancelled' }),
    );

    const { result } = renderHook(() => useCancelOrder(), { wrapper: makeWrapper() });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    act(() => {
      result.current.mutate(orderFixture.id);
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [url, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(url).toBe(`/api/v1/orders/${orderFixture.id}/cancel`);
    expect(init).toMatchObject({ method: 'POST', credentials: 'include' });
    expect(result.current.data?.status).toBe('cancelled');
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['orders'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['schedule', 'capacity-usage'] });
  });
});

// ---------------------------------------------------------------------------
// useTriggerSchedule
// ---------------------------------------------------------------------------

describe('useTriggerSchedule', () => {
  it('POSTs to /schedule/trigger and resolves with task metadata', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ task_id: 't-123', message: 'queued' }),
    );

    const { result } = renderHook(() => useTriggerSchedule(), { wrapper: makeWrapper() });

    act(() => {
      result.current.mutate();
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [url, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(url).toBe('/api/v1/schedule/trigger');
    expect(init).toMatchObject({ method: 'POST', credentials: 'include' });
    expect(result.current.data).toEqual({ task_id: 't-123', message: 'queued' });
  });

  it('does NOT invalidate orderKeys on success (no qc usage)', async () => {
    vi.mocked(global.fetch).mockResolvedValue(jsonResponse({ task_id: 't-1', message: 'queued' }));

    const { result } = renderHook(() => useTriggerSchedule(), { wrapper: makeWrapper() });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    act(() => {
      result.current.mutate();
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useBatchUpdateOrders
// ---------------------------------------------------------------------------

describe('useBatchUpdateOrders', () => {
  it('PATCHes /orders/batch-update with JSON body and invalidates orderKeys only', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ updated_count: 2, skipped_count: 0, skipped_ids: [] }),
    );

    const { result } = renderHook(() => useBatchUpdateOrders(), { wrapper: makeWrapper() });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    const payload = {
      order_ids: ['a-id', 'b-id'],
      update: { status: 'completed' },
    };
    act(() => {
      result.current.mutate(payload as never);
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [url, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(url).toBe('/api/v1/orders/batch-update');
    expect(init).toMatchObject({ method: 'PATCH', credentials: 'include' });
    expect(JSON.parse(init?.body as string)).toEqual(payload);

    expect(result.current.data).toEqual({
      updated_count: 2,
      skipped_count: 0,
      skipped_ids: [],
    });
    // Batch update intentionally does NOT invalidate scheduleCapacityKeys.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['orders'] });
    expect(invalidateSpy).not.toHaveBeenCalledWith({
      queryKey: ['schedule', 'capacity-usage'],
    });
  });

  it('reports skipped ids in the response', async () => {
    const skippedId = '33333333-3333-4333-8333-333333333333';
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ updated_count: 1, skipped_count: 1, skipped_ids: [skippedId] }),
    );

    const { result } = renderHook(() => useBatchUpdateOrders(), { wrapper: makeWrapper() });

    act(() => {
      result.current.mutate({ order_ids: ['x', skippedId], update: {} } as never);
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data?.skipped_ids).toEqual([skippedId]);
  });
});

// ---------------------------------------------------------------------------
// useOrderAuditLog
// ---------------------------------------------------------------------------

describe('useOrderAuditLog', () => {
  it('is disabled when orderId is empty', () => {
    const { result } = renderHook(() => useOrderAuditLog(''), { wrapper: makeWrapper() });
    expect(result.current.fetchStatus).toBe('idle');
    expect(vi.mocked(global.fetch)).not.toHaveBeenCalled();
  });

  it('fetches /orders/{id}/audit-log and validates the response shape', async () => {
    const auditEntry = {
      id: '44444444-4444-4444-8444-444444444444',
      action: 'order.created',
      user_id: '22222222-2222-4222-8222-222222222222',
      resource_id: orderFixture.id,
      old_value: null,
      new_value: { customer_name: 'Acme' },
      created_at: '2026-05-01T00:00:00Z',
    };
    vi.mocked(global.fetch).mockResolvedValue(jsonResponse([auditEntry]));

    const { result } = renderHook(() => useOrderAuditLog(orderFixture.id), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [url, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(url).toBe(`/api/v1/orders/${orderFixture.id}/audit-log`);
    expect(init).toMatchObject({ credentials: 'include' });
    expect(result.current.data).toEqual([auditEntry]);
  });

  it('rejects when the backend returns a payload that fails Zod validation', async () => {
    // Missing required `action` field — Zod parse throws inside apiFetch.
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse([{ id: 'not-a-uuid', resource_id: 'also-bad' }]),
    );

    const { result } = renderHook(() => useOrderAuditLog(orderFixture.id), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });
});
