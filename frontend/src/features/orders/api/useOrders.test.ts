/**
 * useOrders — error handling.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useOrders } from './orders';

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
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  }
  return Wrapper;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useOrders', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    mockAuth.user = { id: 'user-uuid-001', username: 'alice', role: 'scheduler' };
  });

  it('no assigned_to filter is added automatically', async () => {
    const { result } = renderHook(() => useOrders({ page: 1 }), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const url = String((global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]);
    expect(url).not.toContain('assigned_to');
  });

  it('401 response surfaces as an error — no mock data fallback', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Unauthorized' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const { result } = renderHook(() => useOrders({ page: 1 }), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.data).toBeUndefined();
  });
});
