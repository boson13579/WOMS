/**
 * useScheduleCapacity — 30-day capacity prefix sum feed for the dashboard chart.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useScheduleCapacity } from './useScheduleCapacity';

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

function makeEntries(count: number): { date: string; cumulative_remaining: number }[] {
  return Array.from({ length: count }, (_, i) => ({
    date: `2026-05-${String(12 + i).padStart(2, '0')}`,
    cumulative_remaining: (i + 1) * 10000,
  }));
}

const VALID_RESPONSE = {
  base_date: '2026-05-12',
  daily_capacity: 10000,
  entries: makeEntries(30),
};

function mockFetchOnce(body: unknown, status = 200): void {
  vi.mocked(global.fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

describe('useScheduleCapacity', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calls GET /api/v1/schedule/capacity with credentials', async () => {
    mockFetchOnce(VALID_RESPONSE);
    const { result } = renderHook(() => useScheduleCapacity(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/schedule/capacity',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('parses a 30-entry prefix-sum response', async () => {
    mockFetchOnce(VALID_RESPONSE);
    const { result } = renderHook(() => useScheduleCapacity(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data?.entries).toHaveLength(30);
    expect(result.current.data?.daily_capacity).toBe(10000);
    expect(result.current.data?.entries[0].cumulative_remaining).toBe(10000);
  });

  it('rejects entries with negative cumulative_remaining', async () => {
    // Backend enforces ge=0 — frontend Zod must do the same so a bug
    // upstream doesn't get silently rendered.
    mockFetchOnce({
      ...VALID_RESPONSE,
      entries: [{ date: '2026-05-12', cumulative_remaining: -1 }],
    });
    const { result } = renderHook(() => useScheduleCapacity(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });

  it('surfaces backend error envelope on non-2xx', async () => {
    mockFetchOnce({ error: { code: 403, message: 'Insufficient permissions.', details: [] } }, 403);
    const { result } = renderHook(() => useScheduleCapacity(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toBe('Insufficient permissions.');
  });
});
