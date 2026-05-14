/**
 * useSystemHealth — calls /api/v1/system/health and parses the response.
 *
 * Scope per RULES.md §5: cover (a) the happy path, (b) the error path,
 * (c) the schema-rejection path — making sure each path's branching is
 * locked down before we tell components to depend on this hook.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useSystemHealth } from './useSystemHealth';

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => ({ id: 'u', username: 'alice', role: 'viewer' }),
  useCurrentRole: () => 'viewer',
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
  services: [
    {
      id: 'api',
      name: 'API',
      status: 'healthy',
      summary: 'FastAPI · v0.1.0',
      details: [{ label: 'Version', value: '0.1.0' }],
    },
    {
      id: 'postgres',
      name: 'PostgreSQL',
      status: 'healthy',
      summary: 'postgres:15-alpine',
      details: [{ label: 'Latency', value: '2 ms' }],
    },
    {
      id: 'redis',
      name: 'Redis',
      status: 'healthy',
      summary: 'redis:7-alpine',
      details: [{ label: 'Latency', value: '1 ms' }],
    },
    {
      id: 'celery',
      name: 'Celery Worker',
      status: 'healthy',
      summary: 'Scheduler state=idle',
      details: [
        { label: 'State', value: 'idle' },
        { label: 'Queue depth', value: '0' },
      ],
    },
  ],
};

function mockFetchOnce(body: unknown, status = 200): void {
  vi.mocked(global.fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

describe('useSystemHealth', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calls GET /api/v1/system/health with credentials', async () => {
    mockFetchOnce(VALID_RESPONSE);
    const { result } = renderHook(() => useSystemHealth(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/system/health',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('parses a valid response into the 4-service array', async () => {
    mockFetchOnce(VALID_RESPONSE);
    const { result } = renderHook(() => useSystemHealth(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data?.services).toHaveLength(4);
    expect(result.current.data?.services.map((s) => s.id)).toEqual([
      'api',
      'postgres',
      'redis',
      'celery',
    ]);
  });

  it('surfaces backend error envelope message on non-2xx', async () => {
    mockFetchOnce({ error: { code: 401, message: 'Unauthenticated.', details: [] } }, 401);
    const { result } = renderHook(() => useSystemHealth(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toBe('Unauthenticated.');
  });

  it('rejects malformed response (Zod parse fails) — surfaces as query error', async () => {
    // Missing the required ``services`` field.
    mockFetchOnce({ wrong_shape: true });
    const { result } = renderHook(() => useSystemHealth(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });
});
