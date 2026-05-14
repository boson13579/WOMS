/**
 * PendingOpsTable — top-N rows + total footer + Requested-by column.
 *
 * Verifies the truncation contract (N=10 by default), the per-row content,
 * the "queue empty" message, the loading / error branches, and the new
 * username column.
 *
 * Rendering pulls usernames via ``useUsernames`` which talks to
 * ``/system/usernames``. Each test wraps the component in a fresh
 * ``QueryClientProvider`` and mocks ``global.fetch`` to return the
 * expected username payload.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { PendingOpsEntry } from '../types';

import { PendingOpsTable } from './PendingOpsTable';

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

const REQUESTER_ID = '33333333-3333-3333-3333-333333333333';

function makeEntry(rank: number, requestedBy: string = REQUESTER_ID): PendingOpsEntry {
  return {
    compound_id: `11111111-1111-1111-1111-${String(rank).padStart(12, '0')}`,
    rank,
    group: rank % 2 === 0 ? 'shrink' : 'grow',
    op_count: 2,
    ops: [
      {
        op: 'add',
        order_id: `22222222-2222-2222-2222-${String(rank).padStart(12, '0')}`,
        order_number: `ORD-20260512-${String(rank).padStart(4, '0')}`,
      },
      {
        op: 'remove',
        order_id: `22222222-2222-2222-2222-${String(rank).padStart(12, '0')}`,
        order_number: `ORD-20260512-${String(rank).padStart(4, '0')}`,
      },
    ],
    requested_by: requestedBy,
  };
}

function mockUsernameResponse(map: Record<string, string | null>): void {
  vi.mocked(global.fetch).mockResolvedValue(
    new Response(JSON.stringify({ usernames: map }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

describe('PendingOpsTable', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    // Default: every requester resolves to 'alice'. Individual tests
    // override as needed.
    mockUsernameResponse({ [REQUESTER_ID]: 'alice' });
  });

  it('renders all entries when fewer than the truncation cap', () => {
    const entries = [makeEntry(1), makeEntry(2), makeEntry(3)];
    render(<PendingOpsTable data={entries} isLoading={false} isError={false} />, {
      wrapper: makeWrapper(),
    });

    expect(screen.getByText('ORD-20260512-0001')).toBeInTheDocument();
    expect(screen.getByText('ORD-20260512-0002')).toBeInTheDocument();
    expect(screen.getByText('ORD-20260512-0003')).toBeInTheDocument();
    expect(screen.getByText(/3 pending compound/)).toBeInTheDocument();
  });

  it('truncates to topN rows and shows "showing X of total"', () => {
    const entries = Array.from({ length: 1532 }, (_, i) => makeEntry(i + 1));
    render(<PendingOpsTable data={entries} isLoading={false} isError={false} />, {
      wrapper: makeWrapper(),
    });

    expect(screen.getByText('ORD-20260512-0001')).toBeInTheDocument();
    expect(screen.getByText('ORD-20260512-0010')).toBeInTheDocument();
    expect(screen.queryByText('ORD-20260512-0011')).not.toBeInTheDocument();
    expect(screen.getByText(/showing.*10.*of.*1,?532/i)).toBeInTheDocument();
  });

  it('shows the empty-queue message when data is an empty array', () => {
    render(<PendingOpsTable data={[]} isLoading={false} isError={false} />, {
      wrapper: makeWrapper(),
    });
    expect(screen.getByText(/queue is empty/i)).toBeInTheDocument();
  });

  it('renders skeleton while loading', () => {
    render(<PendingOpsTable data={undefined} isLoading isError={false} />, {
      wrapper: makeWrapper(),
    });
    expect(screen.getByTestId('pending-ops-skeleton')).toBeInTheDocument();
  });

  it('renders error message on isError', () => {
    render(<PendingOpsTable data={undefined} isLoading={false} isError />, {
      wrapper: makeWrapper(),
    });
    expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
  });

  it('renders the rank and the group as badges', () => {
    render(<PendingOpsTable data={[makeEntry(7)]} isLoading={false} isError={false} />, {
      wrapper: makeWrapper(),
    });
    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.getByText('grow')).toBeInTheDocument();
  });

  it('renders the op kinds in the Action column joined by →', () => {
    // The default ``makeEntry`` factory builds a compound with [add, remove].
    render(<PendingOpsTable data={[makeEntry(1)]} isLoading={false} isError={false} />, {
      wrapper: makeWrapper(),
    });
    expect(screen.getByText('add → remove')).toBeInTheDocument();
  });

  it('renders the resolved username in the Requested-by column', async () => {
    render(<PendingOpsTable data={[makeEntry(1)]} isLoading={false} isError={false} />, {
      wrapper: makeWrapper(),
    });
    // The fetch is async; wait for the username to land.
    await waitFor(() => {
      expect(screen.getByText('alice')).toBeInTheDocument();
    });
  });

  it('falls back to truncated UUID when the username lookup returns null', async () => {
    mockUsernameResponse({ [REQUESTER_ID]: null });
    render(<PendingOpsTable data={[makeEntry(1)]} isLoading={false} isError={false} />, {
      wrapper: makeWrapper(),
    });
    await waitFor(() => {
      // Truncated UUID = first 8 chars + ellipsis
      expect(screen.getByText('33333333…')).toBeInTheDocument();
    });
  });
});
