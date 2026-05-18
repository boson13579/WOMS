import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAssignableUsers } from './users';

const mockAuth = vi.hoisted(() => ({
  user: {
    id: '11111111-1111-1111-1111-111111111111',
    username: 'scheduler',
    role: 'scheduler',
  } as { id: string; username: string; role: string } | null,
}));

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => mockAuth.user,
  useCurrentRole: () => mockAuth.user?.role ?? null,
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

function mockFetchOnce(body: unknown, status = 200): void {
  vi.mocked(global.fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

describe('useAssignableUsers', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    mockAuth.user = {
      id: '11111111-1111-1111-1111-111111111111',
      username: 'scheduler',
      role: 'scheduler',
    };
  });

  it('loads users from /users/assignable for scheduler/root/order_manager roles', async () => {
    mockFetchOnce([
      {
        id: '22222222-2222-2222-2222-222222222222',
        username: 'alice',
        email: 'alice@example.com',
      },
    ]);

    const { result } = renderHook(() => useAssignableUsers(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current).toHaveLength(1);
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/users/assignable',
      expect.objectContaining({ credentials: 'include' }),
    );
    expect(result.current[0]?.username).toBe('alice');
  });

  it('does not call the assignable endpoint for viewer sessions', () => {
    mockAuth.user = {
      id: '33333333-3333-3333-3333-333333333333',
      username: 'viewer',
      role: 'viewer',
    };

    const { result } = renderHook(() => useAssignableUsers(), { wrapper: makeWrapper() });

    expect(result.current).toEqual([]);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
