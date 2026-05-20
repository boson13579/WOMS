import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useScheduleResult } from './scheduleResult';

const mockAuth = {
  user: { id: 'user-uuid-001', username: 'alice', role: 'scheduler' } as {
    id: string;
    username: string;
    role: string;
  } | null,
  role: 'scheduler' as string | null,
};

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => mockAuth.user,
  useCurrentRole: () => mockAuth.role,
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

const SCHEDULE_RESULT = [
  {
    id: '11111111-1111-4111-8111-111111111111',
    order_number: 'ORD-20260504-0001',
    customer_name: 'TSMC',
    wafer_quantity: 500,
    requested_delivery_date: '2026-06-01',
    scheduled_production_date: '2026-05-09',
    expected_delivery_date: '2026-05-10',
    status: 'scheduled',
    daily_breakdown: [{ date: '2026-05-09', quantity: 500 }],
  },
];

describe('useScheduleResult', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    mockAuth.user = { id: 'user-uuid-001', username: 'alice', role: 'scheduler' };
    mockAuth.role = 'scheduler';
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify(SCHEDULE_RESULT), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });

  it('calls GET /api/v1/schedule/result with credentials', async () => {
    const { result } = renderHook(() => useScheduleResult(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/schedule/result',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('does not fetch for viewer role', () => {
    mockAuth.role = 'viewer';
    mockAuth.user = { id: 'viewer-id', username: 'victor', role: 'viewer' };

    const { result } = renderHook(() => useScheduleResult(), { wrapper: makeWrapper() });

    expect(result.current.fetchStatus).toBe('idle');
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('rejects malformed schedule result payloads', async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify([{ ...SCHEDULE_RESULT[0], expected_delivery_date: 123 }]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const { result } = renderHook(() => useScheduleResult(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });
});
