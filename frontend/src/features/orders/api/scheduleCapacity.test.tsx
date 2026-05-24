import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { toDailyCapacity, useScheduleCapacity } from './scheduleCapacity';

vi.mock('@/lib/auth', () => ({
  useCurrentRole: () => 'scheduler',
  useCurrentUser: () => ({ id: '33333333-3333-4333-8333-333333333333' }),
}));

let qc: QueryClient;

function makeWrapper() {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return Wrapper;
}

describe('useScheduleCapacity', () => {
  beforeEach(() => {
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          base_date: '2026-05-19',
          daily_capacity: 2500,
          entries: [
            { date: '2026-05-19', used: 500, remaining: 2000 },
            { date: '2026-05-20', used: 1500, remaining: 1000 },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
  });

  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  it('fetches scheduler capacity usage', async () => {
    const { result } = renderHook(() => useScheduleCapacity(), { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/schedule/capacity-usage',
      expect.objectContaining({ credentials: 'include' }),
    );
    expect(result.current.data?.daily_capacity).toBe(2500);
  });

  it('maps daily capacity usage without recalculating remaining capacity', () => {
    expect(
      toDailyCapacity({
        base_date: '2026-05-19',
        daily_capacity: 2500,
        entries: [
          { date: '2026-05-19', used: 500, remaining: 2000 },
          { date: '2026-05-20', used: 1500, remaining: 1000 },
          { date: '2026-05-21', used: 2500, remaining: 0 },
        ],
      }),
    ).toEqual([
      { date: '2026-05-19', used: 500, remaining: 2000, dailyCapacity: 2500 },
      { date: '2026-05-20', used: 1500, remaining: 1000, dailyCapacity: 2500 },
      { date: '2026-05-21', used: 2500, remaining: 0, dailyCapacity: 2500 },
    ]);
  });
});
