/**
 * useOrders — role-based assigned_to filtering.
 *
 * Verifies that the hook injects `assigned_to=<userId>` for non-root users
 * and omits it for root users.
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
  role: 'scheduler' as string | null,
  userId: 'user-uuid-001' as string | null,
};

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => mockAuth.user,
  useCurrentRole: () => mockAuth.role,
  useCurrentUserId: () => mockAuth.userId,
  useCanWrite: () => mockAuth.role === 'scheduler' || mockAuth.role === 'root',
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

function capturedUrl(): string {
  const { calls } = (global.fetch as ReturnType<typeof vi.fn>).mock;
  return String(calls[calls.length - 1][0]);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useOrders — role-based assigned_to filtering', () => {
  afterEach(() => {
    cleanup(); // unmount hook first (removes observer), then clear cache
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
    mockAuth.role = 'scheduler';
    mockAuth.userId = 'user-uuid-001';
  });

  it('scheduler — adds assigned_to=userId to the request', async () => {
    const { result } = renderHook(() => useOrders({ page: 1 }), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(capturedUrl()).toContain('assigned_to=user-uuid-001');
  });

  it('order_manager — adds assigned_to=userId to the request', async () => {
    mockAuth.role = 'order_manager';
    mockAuth.user = { id: 'user-uuid-001', username: 'alice', role: 'order_manager' };

    const { result } = renderHook(() => useOrders({ page: 1 }), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(capturedUrl()).toContain('assigned_to=user-uuid-001');
  });

  it('root — does NOT add assigned_to (sees all orders)', async () => {
    mockAuth.role = 'root';
    mockAuth.user = { id: 'user-uuid-001', username: 'admin', role: 'root' };

    const { result } = renderHook(() => useOrders({ page: 1 }), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(capturedUrl()).not.toContain('assigned_to');
  });
});
