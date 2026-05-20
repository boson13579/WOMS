/**
 * Tests for the React Query-backed ``useMe()`` hook that fronts
 * ``GET /api/v1/auth/me``.
 *
 * Server-confirmed identity is what frees the SPA from local-clock
 * trust on the persisted ``expiresAt``. The hook keeps the answer in
 * the cache for 30 s and refetches on window focus so role/active
 * changes propagate within one focus cycle.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useMe } from './auth';

const VALID_ME = {
  id: '00000000-0000-0000-0000-000000000002',
  username: 'alice',
  email: 'alice@example.com',
  role: 'viewer' as const,
  is_active: true,
  version_id: 4,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

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

beforeEach(() => {
  vi.spyOn(globalThis, 'fetch');
});

afterEach(() => {
  cleanup();
  qc.clear();
  vi.restoreAllMocks();
});

describe('useMe', () => {
  // RED: useMe doesn't exist yet.
  it('queries /api/v1/auth/me and returns the parsed user on success', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(jsonResponse(VALID_ME));

    const { result } = renderHook(() => useMe(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data?.id).toBe(VALID_ME.id);
    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/auth/me',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('caches under queryKey ["auth","me"]', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(jsonResponse(VALID_ME));

    const { result } = renderHook(() => useMe(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(qc.getQueryData(['auth', 'me'])).toEqual(VALID_ME);
  });
});
