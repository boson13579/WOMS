/**
 * useRedMetrics — verifies the query key includes ``window_seconds``,
 * the response is parsed by Zod (malformed payload → ``isError``),
 * and a successful poll appends to the ring buffer.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useRedHistoryStore } from '../stores/redHistoryStore';

import { redMetricsQueryKey, useRedMetrics } from './useRedMetrics';

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => ({ id: 'u', username: 'alice', role: 'scheduler' }),
  useCurrentRole: () => 'scheduler',
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

const VALID_RESPONSE = {
  window_seconds: 60,
  total_requests: 744,
  rate_per_sec: 12.4,
  error_count: 3,
  error_pct: 0.4,
  latency_ms: { p50: 12, p95: 45, p99: 95, max: 320 },
  by_endpoint: [],
};

function mockFetchOnce(body: unknown, status = 200): void {
  vi.mocked(global.fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

describe('useRedMetrics', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
    useRedHistoryStore.getState().reset();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    useRedHistoryStore.getState().reset();
  });

  it('hits /system/red with the window_seconds query param', async () => {
    mockFetchOnce(VALID_RESPONSE);
    const { result } = renderHook(() => useRedMetrics(60), { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/system/red?window_seconds=60',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('uses a different query key per window_seconds value', () => {
    const k60 = redMetricsQueryKey(60);
    const k300 = redMetricsQueryKey(300);
    expect(k60).not.toEqual(k300);
    expect(k60).toEqual(['system', 'red', 60]);
    expect(k300).toEqual(['system', 'red', 300]);
  });

  it('surfaces a malformed payload as isError (zod parse fails)', async () => {
    // Missing the required ``latency_ms`` field.
    mockFetchOnce({ window_seconds: 60, total_requests: 1, rate_per_sec: 0.0 });
    const { result } = renderHook(() => useRedMetrics(60), { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });

  it('appends the rate / error_pct / p95 to the ring buffer on success', async () => {
    mockFetchOnce(VALID_RESPONSE);
    const { result } = renderHook(() => useRedMetrics(60), { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const { series } = useRedHistoryStore.getState();
    expect(series.rate.at(-1)).toBeCloseTo(12.4);
    expect(series.errorPct.at(-1)).toBeCloseTo(0.4);
    expect(series.p95.at(-1)).toBe(45);
  });
});
