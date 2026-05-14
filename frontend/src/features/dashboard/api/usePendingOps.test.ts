/**
 * usePendingOps — fetches the pending-ops queue (top-N + total).
 *
 * Endpoint returns the **full** queue today; for dashboard we only display
 * the first N rows and a "showing X of total" footer. The hook leaves the
 * full list to the caller — slicing happens at render time so the
 * unsliced response is also available if a future "expand all" UI lands.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { usePendingOps } from './usePendingOps';

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => ({ id: 'u', username: 'alice', role: 'order_manager' }),
  useCurrentRole: () => 'order_manager',
}));

let qc: QueryClient;

function makeWrapper() {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  }
  return Wrapper;
}

const VALID_ENTRY = {
  compound_id: '11111111-1111-1111-1111-111111111111',
  rank: 1,
  group: 'shrink',
  op_count: 2,
  ops: [
    {
      op: 'unpin',
      order_id: '22222222-2222-2222-2222-222222222222',
      order_number: 'ORD-20260512-0001',
    },
    {
      op: 'remove',
      order_id: '22222222-2222-2222-2222-222222222222',
      order_number: 'ORD-20260512-0001',
    },
  ],
  requested_by: '33333333-3333-3333-3333-333333333333',
};

function mockFetchOnce(body: unknown, status = 200): void {
  vi.mocked(global.fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

describe('usePendingOps', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calls GET /api/v1/schedule/pending-ops with credentials', async () => {
    mockFetchOnce([VALID_ENTRY]);
    const { result } = renderHook(() => usePendingOps(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/schedule/pending-ops',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('parses a list of pending compounds', async () => {
    mockFetchOnce([VALID_ENTRY]);
    const { result } = renderHook(() => usePendingOps(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data).toHaveLength(1);
    expect(result.current.data?.[0].rank).toBe(1);
    expect(result.current.data?.[0].ops).toHaveLength(2);
  });

  it('handles empty queue', async () => {
    mockFetchOnce([]);
    const { result } = renderHook(() => usePendingOps(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data).toEqual([]);
  });

  it('rejects entries with rank < 1 (backend invariant)', async () => {
    mockFetchOnce([{ ...VALID_ENTRY, rank: 0 }]);
    const { result } = renderHook(() => usePendingOps(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });

  it('surfaces backend error envelope on non-2xx', async () => {
    mockFetchOnce({ error: { code: 403, message: 'Insufficient permissions.', details: [] } }, 403);
    const { result } = renderHook(() => usePendingOps(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toBe('Insufficient permissions.');
  });
});
