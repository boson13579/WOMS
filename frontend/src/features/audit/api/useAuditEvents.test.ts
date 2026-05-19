/**
 * useAuditEvents — URL construction + query key + enable gating.
 *
 * Mirrors the testing pattern from `useOrders.test.ts`: mocks
 * `@/lib/auth` so the role gate can be flipped per case, and
 * inspects the fetch URL to assert the params we serialise.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  auditEventsQueryKey,
  buildAuditEventsUrl,
  useAuditEvents,
  type UseAuditEventsParams,
} from './useAuditEvents';

const mockAuth = { role: 'root' as 'root' | 'scheduler' | 'order_manager' | 'viewer' | null };

vi.mock('@/lib/auth', () => ({
  useCurrentRole: () => mockAuth.role,
}));

let qc: QueryClient;

function makeWrapper() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  }
  return Wrapper;
}

const EMPTY_RESPONSE = {
  items: [],
  total: 0,
  page: 1,
  page_size: 20,
};

function jsonOnce(body: unknown, status = 200): void {
  vi.mocked(global.fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

describe('useAuditEvents — URL builder', () => {
  it('includes page and page_size; omits empty filters', () => {
    const url = buildAuditEventsUrl({ page: 1, pageSize: 20 });
    expect(url).toBe('/api/v1/audit/events?page=1&page_size=20');
    expect(url).not.toContain('actor_id');
    expect(url).not.toContain('action');
    expect(url).not.toContain('resource_type');
    expect(url).not.toContain('from');
    expect(url).not.toContain('to');
  });

  it('encodes every filter when present', () => {
    const url = buildAuditEventsUrl({
      page: 2,
      pageSize: 50,
      actorId: '11111111-1111-1111-1111-111111111111',
      action: 'order.created',
      resourceType: 'order',
      fromDate: '2026-05-01',
      toDate: '2026-05-20',
    });
    expect(url).toContain('actor_id=11111111-1111-1111-1111-111111111111');
    expect(url).toContain('action=order.created');
    expect(url).toContain('resource_type=order');
    // T00:00:00Z encoded as %3A%3A
    expect(url).toContain('from=2026-05-01T00%3A00%3A00Z');
    expect(url).toContain('to=2026-05-20T23%3A59%3A59Z');
    expect(url).toContain('page=2');
    expect(url).toContain('page_size=50');
  });

  it('omits resource_type when the synthetic "other" option is selected', () => {
    const url = buildAuditEventsUrl({
      page: 1,
      pageSize: 20,
      resourceType: 'other',
    });
    expect(url).not.toContain('resource_type');
  });
});

describe('useAuditEvents — query key', () => {
  it('includes every filter in the query key for cache scoping', () => {
    const params: UseAuditEventsParams = {
      page: 3,
      pageSize: 50,
      actorId: 'actor-uuid',
      action: 'user.login_succeeded',
      resourceType: 'user',
      fromDate: '2026-05-01',
      toDate: '2026-05-20',
    };
    expect(auditEventsQueryKey(params)).toEqual(['audit', 'events', params]);
  });
});

describe('useAuditEvents — hook behaviour', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
    mockAuth.role = 'root';
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('fetches /audit/events when the role is root', async () => {
    jsonOnce(EMPTY_RESPONSE);
    const { result } = renderHook(() => useAuditEvents({ page: 1, pageSize: 20 }), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const url = String((global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]);
    expect(url).toContain('/api/v1/audit/events');
    expect(url).toContain('page=1');
    expect(url).toContain('page_size=20');
  });

  it('does not fetch when the role is not root (defence in depth)', async () => {
    mockAuth.role = 'scheduler';
    const fetchSpy = vi.mocked(global.fetch);
    fetchSpy.mockClear();

    const { result } = renderHook(() => useAuditEvents({ page: 1, pageSize: 20 }), {
      wrapper: makeWrapper(),
    });

    // Give React Query a tick; it should not flip to loading because enabled=false.
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });

    expect(result.current.fetchStatus).toBe('idle');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('401 response surfaces as an error', async () => {
    jsonOnce({ detail: 'Unauthorized' }, 401);
    const { result } = renderHook(() => useAuditEvents({ page: 1, pageSize: 20 }), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.data).toBeUndefined();
  });

  it('keeps previous data across page changes (placeholderData honoured)', async () => {
    jsonOnce({
      items: [],
      total: 100,
      page: 1,
      page_size: 20,
    });
    const { result, rerender } = renderHook(({ page }) => useAuditEvents({ page, pageSize: 20 }), {
      wrapper: makeWrapper(),
      initialProps: { page: 1 },
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const firstData = result.current.data;
    expect(firstData?.total).toBe(100);

    // Now page → 2. Before the network resolves, data should still reflect page 1.
    let resolveSecond!: (response: Response) => void;
    vi.mocked(global.fetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveSecond = resolve;
      }),
    );
    rerender({ page: 2 });
    // While the next page is in flight, `data` should remain page 1.
    expect(result.current.data).toBe(firstData);

    // Resolve the second page; data should update.
    resolveSecond(
      new Response(JSON.stringify({ items: [], total: 100, page: 2, page_size: 20 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await waitFor(() => {
      expect(result.current.data?.page).toBe(2);
    });
  });
});
