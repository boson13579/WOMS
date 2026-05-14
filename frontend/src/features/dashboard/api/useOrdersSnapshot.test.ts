/**
 * useOrdersSnapshot — 4 parallel `GET /orders?status=X` calls combined.
 *
 * The hook makes one HTTP request per dashboard-displayed status
 * (pending / scheduled / in_production / completed), reads ``total`` off
 * each response, and aggregates into a single counts object. Tests cover
 * the parallel-call pattern, the combined success shape, and the
 * any-fails-everything-fails error semantics.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useOrdersSnapshot } from './useOrdersSnapshot';

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

/**
 * Configure fetch mock to return different ``total`` values for each
 * status filter, simulating the real backend pattern of one count per
 * status. The mock inspects the URL's ``status`` query param.
 */
function setupFetchPerStatus(totals: Record<string, number>): void {
  vi.mocked(global.fetch).mockImplementation((url) => {
    const u = new URL(String(url), 'http://localhost');
    const status = u.searchParams.get('status') ?? '';
    return Promise.resolve(
      new Response(
        JSON.stringify({
          items: [],
          total: totals[status] ?? 0,
          page: 1,
          page_size: 1,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
  });
}

describe('useOrdersSnapshot', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('issues one request per status (4 total)', async () => {
    setupFetchPerStatus({ pending: 3, scheduled: 96, in_production: 12, completed: 65 });
    const { result } = renderHook(() => useOrdersSnapshot(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    const urls = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
    expect(urls.filter((u) => u.includes('status=pending'))).toHaveLength(1);
    expect(urls.filter((u) => u.includes('status=scheduled'))).toHaveLength(1);
    expect(urls.filter((u) => u.includes('status=in_production'))).toHaveLength(1);
    expect(urls.filter((u) => u.includes('status=completed'))).toHaveLength(1);
  });

  it('combines totals into a single counts object', async () => {
    setupFetchPerStatus({ pending: 3, scheduled: 96, in_production: 12, completed: 65 });
    const { result } = renderHook(() => useOrdersSnapshot(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data).toEqual({
      pending: 3,
      scheduled: 96,
      in_production: 12,
      completed: 65,
    });
  });

  it('reports isError if any single status query fails', async () => {
    vi.mocked(global.fetch).mockImplementation((url) => {
      const u = new URL(String(url), 'http://localhost');
      const status = u.searchParams.get('status') ?? '';
      if (status === 'in_production') {
        return Promise.resolve(
          new Response(
            JSON.stringify({ error: { code: 500, message: 'DB error.', details: [] } }),
            { status: 500, headers: { 'Content-Type': 'application/json' } },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 1 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    });
    const { result } = renderHook(() => useOrdersSnapshot(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });

  it('reflects background refetches via isFetching after the first load resolves', async () => {
    // ``isLoading`` flips false the moment cached data exists; only
    // ``isFetching`` stays true through background refetches (Refresh
    // button, polling tick). The Header's spinner aggregation in
    // DashboardPage depends on this distinction — assert it explicitly
    // so a future regression to ``isLoading`` semantics is caught.

    // First load — synchronous mock, resolves immediately.
    setupFetchPerStatus({ pending: 1, scheduled: 1, in_production: 1, completed: 1 });
    const { result } = renderHook(() => useOrdersSnapshot(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.isFetching).toBe(false);

    // Swap to a deferred mock so we can observe the in-flight state
    // before letting the refetch complete.
    let releaseRefetch: () => void = () => {};
    const refetchPending = new Promise<void>((resolve) => {
      releaseRefetch = resolve;
    });
    vi.mocked(global.fetch).mockImplementation(async () => {
      await refetchPending;
      return new Response(JSON.stringify({ items: [], total: 1, page: 1, page_size: 1 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    void qc.invalidateQueries({ queryKey: ['orders', 'snapshot'] });

    await waitFor(() => {
      expect(result.current.isFetching).toBe(true);
    });
    // Cached data is still there, so isLoading stays false — this is
    // exactly the case the Header spinner was missing before.
    expect(result.current.isLoading).toBe(false);

    releaseRefetch();
    await waitFor(() => {
      expect(result.current.isFetching).toBe(false);
    });
  });

  it('requests page_size=1 (counts only, no items needed)', async () => {
    setupFetchPerStatus({ pending: 3, scheduled: 96, in_production: 12, completed: 65 });
    renderHook(() => useOrdersSnapshot(), { wrapper: makeWrapper() });

    await waitFor(() => {
      const { calls } = (global.fetch as ReturnType<typeof vi.fn>).mock;
      expect(calls.length).toBeGreaterThanOrEqual(4);
    });
    const urls = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
    urls.forEach((url) => {
      expect(url).toContain('page_size=1');
    });
  });
});
