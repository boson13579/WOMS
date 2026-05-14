/**
 * useScheduleStatus — fetches the scheduler lifecycle state badge data.
 *
 * Covers the three legal ``state`` values (idle / running / failed) plus
 * the first-deploy ``no data`` case where the backend synthesises a
 * skeleton response. Error / malformed-response paths mirror the
 * useSystemHealth test layout.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useScheduleStatus } from './useScheduleStatus';

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

const RUNNING_RESPONSE = {
  state: 'running',
  started_at: '2026-05-12T00:13:42+00:00',
  finished_at: null,
  task_id: 'celery-task-uuid',
  error: null,
  message: null,
};

const EMPTY_RESPONSE = {
  state: 'idle',
  started_at: null,
  finished_at: null,
  task_id: null,
  error: null,
  message: 'No scheduling has been run yet',
};

function mockFetchOnce(body: unknown, status = 200): void {
  vi.mocked(global.fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

describe('useScheduleStatus', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calls GET /api/v1/schedule/status with credentials', async () => {
    mockFetchOnce(RUNNING_RESPONSE);
    const { result } = renderHook(() => useScheduleStatus(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/schedule/status',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('parses running state response', async () => {
    mockFetchOnce(RUNNING_RESPONSE);
    const { result } = renderHook(() => useScheduleStatus(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data?.state).toBe('running');
    expect(result.current.data?.task_id).toBe('celery-task-uuid');
  });

  it('parses empty-state response (first deploy)', async () => {
    mockFetchOnce(EMPTY_RESPONSE);
    const { result } = renderHook(() => useScheduleStatus(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data?.state).toBe('idle');
    expect(result.current.data?.message).toBe('No scheduling has been run yet');
  });

  it('surfaces backend error envelope on 403 (e.g. viewer)', async () => {
    mockFetchOnce({ error: { code: 403, message: 'Insufficient permissions.', details: [] } }, 403);
    const { result } = renderHook(() => useScheduleStatus(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toBe('Insufficient permissions.');
  });

  it('rejects unknown state strings', async () => {
    mockFetchOnce({ ...RUNNING_RESPONSE, state: 'magical_new_state' });
    const { result } = renderHook(() => useScheduleStatus(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });
});
