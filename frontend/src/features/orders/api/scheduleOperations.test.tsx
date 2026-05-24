import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { act, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { usePinScheduleOperation } from './scheduleOperations';

const mockAuth = {
  userId: '33333333-3333-4333-8333-333333333333' as string | null,
};

vi.mock('@/lib/auth', () => ({
  useCurrentUserId: () => mockAuth.userId,
}));

let qc: QueryClient;

function makeWrapper() {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return Wrapper;
}

const baseOrder = {
  id: '11111111-1111-4111-8111-111111111111',
  order_number: 'ORD-20260504-0001',
  wafer_quantity: 500,
  requested_delivery_date: '2026-06-01',
  status: 'scheduled' as const,
  is_pinned: false,
  pinned_production_date: null,
  version_id: 1,
};

function mockPatchOk(orderId: string, versionId = 2): Response {
  return new Response(JSON.stringify({ id: orderId, version_id: versionId }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('usePinScheduleOperation (PATCH /orders/{id} flow)', () => {
  beforeEach(() => {
    mockAuth.userId = '33333333-3333-4333-8333-333333333333';
    // Mock is sync (returns the Response synchronously) but vi.fn().mockImplementation
    // wraps it for the async fetch signature. Use a non-async function and
    // wrap the return in Promise.resolve to satisfy both fetch's Promise
    // return type and lint's "no unused async" rule.
    vi.mocked(global.fetch).mockImplementation((input) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      // Extract order id from URL path: /api/v1/orders/{id}
      const m = /\/api\/v1\/orders\/([^/?]+)/.exec(url);
      const orderId = m?.[1] ?? 'unknown';
      return Promise.resolve(mockPatchOk(orderId));
    });
  });

  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  it('PATCHes /orders/{id} with pinned_production_date when pinning to a day', async () => {
    const { result } = renderHook(() => usePinScheduleOperation(), { wrapper: makeWrapper() });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');

    act(() => {
      result.current.mutate({
        compoundId: '44444444-4444-4444-8444-444444444444',
        targets: [{ order: baseOrder, targetDate: '2026-05-10' }],
      });
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    // Single PATCH against the order's own endpoint, not /schedule/operations.
    const [url, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(url).toBe(`/api/v1/orders/${baseOrder.id}`);
    expect(init).toMatchObject({ method: 'PATCH', credentials: 'include' });
    expect(JSON.parse(init?.body as string)).toEqual({
      pinned_production_date: '2026-05-10',
      version_id: 1,
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['orders'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['schedule', 'capacity-usage'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['schedule', 'result'] });
  });

  it('PATCHes with pinned_production_date=null when targetDate is null (unpin)', async () => {
    const { result } = renderHook(() => usePinScheduleOperation(), { wrapper: makeWrapper() });

    act(() => {
      result.current.mutate({
        compoundId: '44444444-4444-4444-8444-444444444444',
        targets: [
          {
            order: { ...baseOrder, is_pinned: true, pinned_production_date: '2026-05-09' },
            targetDate: null,
          },
        ],
      });
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(JSON.parse(init?.body as string)).toEqual({
      pinned_production_date: null,
      version_id: 1,
    });
  });

  it('fans out parallel PATCHes for multi-target pin (one per order)', async () => {
    const { result } = renderHook(() => usePinScheduleOperation(), { wrapper: makeWrapper() });

    const order2 = {
      ...baseOrder,
      id: '22222222-2222-4222-8222-222222222222',
      order_number: 'ORD-20260504-0002',
      version_id: 4,
    };

    act(() => {
      result.current.mutate({
        compoundId: '44444444-4444-4444-8444-444444444444',
        targets: [
          { order: baseOrder, targetDate: '2026-05-10' },
          { order: order2, targetDate: '2026-05-11' },
        ],
      });
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    // Two PATCH calls, one per order. Each carries that order's own
    // version_id and target date — no shared compound payload.
    const { calls } = vi.mocked(global.fetch).mock;
    expect(calls).toHaveLength(2);

    const urls = calls.map((c) => c[0]).sort();
    expect(urls).toEqual([`/api/v1/orders/${baseOrder.id}`, `/api/v1/orders/${order2.id}`].sort());

    const bodies = calls.map((c) => JSON.parse(c[1]?.body as string) as Record<string, unknown>);
    const byOrder = Object.fromEntries(
      calls.map((c, i) => {
        const parts = (c[0] as string).split('/');
        return [parts[parts.length - 1], bodies[i]];
      }),
    );
    expect(byOrder[baseOrder.id]).toEqual({
      pinned_production_date: '2026-05-10',
      version_id: 1,
    });
    expect(byOrder[order2.id]).toEqual({
      pinned_production_date: '2026-05-11',
      version_id: 4,
    });
  });

  it('rejects the mutation if one of the PATCHes fails', async () => {
    vi.mocked(global.fetch).mockImplementation((input) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      // First call ok, second call 409 (concurrent PATCH bumped version_id).
      if (url.endsWith(baseOrder.id)) {
        return Promise.resolve(mockPatchOk(baseOrder.id));
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({
            error: {
              code: 409,
              message: 'Order was modified by another user. Refresh and try again.',
            },
          }),
          { status: 409, headers: { 'Content-Type': 'application/json' } },
        ),
      );
    });

    const { result } = renderHook(() => usePinScheduleOperation(), { wrapper: makeWrapper() });

    const order2 = {
      ...baseOrder,
      id: '22222222-2222-4222-8222-222222222222',
      order_number: 'ORD-20260504-0002',
    };

    act(() => {
      result.current.mutate({
        compoundId: '44444444-4444-4444-8444-444444444444',
        targets: [
          { order: baseOrder, targetDate: '2026-05-10' },
          { order: order2, targetDate: '2026-05-11' },
        ],
      });
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toContain('modified by another user');
  });
});
