import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { act, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { usePinScheduleOperation } from './scheduleOperations';

const mockAuth = {
  userId: '33333333-3333-4333-8333-333333333333' as string | null,
};

vi.mock('@/lib/auth', () => ({
  useCurrentUserId: () => mockAuth.userId,
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

const baseOrder = {
  id: '11111111-1111-4111-8111-111111111111',
  order_number: 'ORD-20260504-0001',
  wafer_quantity: 500,
  requested_delivery_date: '2026-06-01',
  status: 'scheduled' as const,
  is_pinned: false,
  pinned_production_date: null,
};

describe('usePinScheduleOperation', () => {
  beforeEach(() => {
    mockAuth.userId = '33333333-3333-4333-8333-333333333333';
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          compound_id: '44444444-4444-4444-8444-444444444444',
          message: 'Compound queued',
        }),
        { status: 202, headers: { 'Content-Type': 'application/json' } },
      ),
    );
  });

  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  it('queues a pin compound for an existing scheduled order', async () => {
    const { result } = renderHook(() => usePinScheduleOperation(), { wrapper: makeWrapper() });

    act(() => {
      result.current.mutate({
        compoundId: '44444444-4444-4444-8444-444444444444',
        targets: [{ order: baseOrder, targetDate: '2026-05-10' }],
      });
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/schedule/operations',
      expect.objectContaining({ method: 'POST', credentials: 'include' }),
    );
    expect(JSON.parse(init?.body as string)).toMatchObject({
      compound_id: '44444444-4444-4444-8444-444444444444',
      group: 'grow',
      op_count: 1,
      requested_by: '33333333-3333-4333-8333-333333333333',
      ops: [
        {
          op: 'pin',
          order_id: baseOrder.id,
          order_number: baseOrder.order_number,
          wafer_quantity: 500,
          deadline: '2026-06-01',
          fake_deadline: '2026-05-10',
        },
      ],
    });
  });

  it('queues add then pin for an unscheduled pending order', async () => {
    const { result } = renderHook(() => usePinScheduleOperation(), { wrapper: makeWrapper() });

    act(() => {
      result.current.mutate({
        compoundId: '44444444-4444-4444-8444-444444444444',
        targets: [{ order: { ...baseOrder, status: 'pending' }, targetDate: '2026-05-10' }],
      });
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [, init] = vi.mocked(global.fetch).mock.calls[0];
    const body = JSON.parse(init?.body as string) as { op_count: number; ops: { op: string }[] };
    expect(body.ops.map((op) => op.op)).toEqual(['add', 'pin']);
    expect(body.op_count).toBe(2);
  });

  it('queues unpin then pin for an already pinned order', async () => {
    const { result } = renderHook(() => usePinScheduleOperation(), { wrapper: makeWrapper() });

    act(() => {
      result.current.mutate({
        compoundId: '44444444-4444-4444-8444-444444444444',
        targets: [
          {
            order: { ...baseOrder, is_pinned: true, pinned_production_date: '2026-05-09' },
            targetDate: '2026-05-10',
          },
        ],
      });
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [, init] = vi.mocked(global.fetch).mock.calls[0];
    const body = JSON.parse(init?.body as string) as { op_count: number; ops: { op: string }[] };
    expect(body.ops.map((op) => op.op)).toEqual(['unpin', 'pin']);
    expect(body.op_count).toBe(2);
  });

  it('queues multiple orders in one ordered compound', async () => {
    const { result } = renderHook(() => usePinScheduleOperation(), { wrapper: makeWrapper() });

    act(() => {
      result.current.mutate({
        compoundId: '44444444-4444-4444-8444-444444444444',
        targets: [
          { order: { ...baseOrder, status: 'pending' }, targetDate: '2026-05-10' },
          {
            order: {
              ...baseOrder,
              id: '22222222-2222-4222-8222-222222222222',
              order_number: 'ORD-20260504-0002',
              status: 'pending',
            },
            targetDate: '2026-05-11',
          },
        ],
      });
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    const [, init] = vi.mocked(global.fetch).mock.calls[0];
    const body = JSON.parse(init?.body as string) as {
      op_count: number;
      ops: { op: string; order_number: string }[];
    };
    expect(body.ops.map((op) => `${op.order_number}:${op.op}`)).toEqual([
      'ORD-20260504-0001:add',
      'ORD-20260504-0001:pin',
      'ORD-20260504-0002:add',
      'ORD-20260504-0002:pin',
    ]);
    expect(body.ops.map((op) => ('fake_deadline' in op ? op.fake_deadline : null))).toEqual([
      null,
      '2026-05-10',
      null,
      '2026-05-11',
    ]);
    expect(body.op_count).toBe(4);
  });
});
