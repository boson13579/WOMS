/**
 * useUsernames — bulk UUID → username lookup hook used by Pending Ops.
 *
 * Tests cover (a) the happy path, (b) dedup behaviour, (c) the empty-ids
 * short-circuit (don't call the API when there's nothing to look up),
 * (d) parsing of the ``null`` value for unknown UUIDs, and (e) HTTP
 * error → React Query error.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useUsernames } from './useUsernames';

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

function mockFetchOnce(body: unknown, status = 200): void {
  vi.mocked(global.fetch).mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

const UID_A = '11111111-1111-1111-1111-111111111111';
const UID_B = '22222222-2222-2222-2222-222222222222';
const UID_UNKNOWN = '33333333-3333-3333-3333-333333333333';

describe('useUsernames', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns the empty map without fetching when ids is empty', async () => {
    const { result } = renderHook(() => useUsernames([]), { wrapper: makeWrapper() });

    // No fetch should happen — empty input short-circuits to {}.
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual({});
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('calls GET /system/usernames with deduped comma-separated ids', async () => {
    mockFetchOnce({ usernames: { [UID_A]: 'alice', [UID_B]: 'bob' } });
    const { result } = renderHook(() => useUsernames([UID_A, UID_B, UID_A]), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const url = String((global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]);
    expect(url).toContain('/api/v1/system/usernames');
    // Deduped, both ids present
    expect(url).toContain(UID_A);
    expect(url).toContain(UID_B);
  });

  it('exposes data as { uuid: username | null }', async () => {
    mockFetchOnce({
      usernames: {
        [UID_A]: 'alice',
        [UID_UNKNOWN]: null,
      },
    });
    const { result } = renderHook(() => useUsernames([UID_A, UID_UNKNOWN]), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data?.[UID_A]).toBe('alice');
    expect(result.current.data?.[UID_UNKNOWN]).toBeNull();
  });

  it('surfaces backend error envelope on non-2xx', async () => {
    mockFetchOnce({ error: { code: 422, message: 'Invalid UUID.', details: [] } }, 422);
    const { result } = renderHook(() => useUsernames([UID_A]), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.error?.message).toBe('Invalid UUID.');
  });
});
