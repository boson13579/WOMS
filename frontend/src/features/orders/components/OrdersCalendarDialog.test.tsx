import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Order, ScheduleResult } from '../types';

import { OrdersCalendarDialog } from './OrdersCalendarDialog';

const mockScheduleResult = {
  data: [] as ScheduleResult[],
  isPending: false,
  isError: false,
};

const mockOrders = {
  data: { items: [] as Order[], total: 0, page: 1, page_size: 100 },
  isPending: false,
  isError: false,
};

const mockScheduledOrders = {
  data: { items: [] as Order[], total: 0, page: 1, page_size: 100 },
  isPending: false,
  isFetching: false,
  isSuccess: true,
  isError: false,
};

const mockRole = { value: 'scheduler' as string | null };
interface PinMutationOptions {
  onSuccess?: () => void;
  onError?: (error: Error) => void;
}

const mockPinMutate = vi.fn();
const mockPinSchedule = {
  mutate: mockPinMutate,
  isPending: false,
};

vi.mock('@/lib/auth', () => ({
  useCurrentRole: () => mockRole.value,
  useCurrentUserId: () => '33333333-3333-4333-8333-333333333333',
}));

vi.mock('../api/scheduleResult', () => ({
  useScheduleResult: () => mockScheduleResult,
}));

vi.mock('../api/orders', () => ({
  useOrders: (params: { status?: string }) =>
    params.status === 'scheduled' ? mockScheduledOrders : mockOrders,
}));

vi.mock('../api/scheduleOperations', () => ({
  usePinScheduleOperation: () => mockPinSchedule,
}));

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return Wrapper;
}

function renderDialog(): void {
  render(<OrdersCalendarDialog open onOpenChange={vi.fn()} />, { wrapper: makeWrapper() });
}

const scheduledOrder: ScheduleResult = {
  id: '11111111-1111-4111-8111-111111111111',
  order_number: 'ORD-20260504-0001',
  customer_name: 'TSMC',
  wafer_quantity: 500,
  requested_delivery_date: '2026-06-01',
  scheduled_production_date: '2026-05-09',
  expected_delivery_date: '2026-05-10',
  status: 'scheduled',
  daily_breakdown: [{ date: '2026-05-09', quantity: 500 }],
};

const pendingOrder: Order = {
  id: '22222222-2222-4222-8222-222222222222',
  order_number: 'ORD-20260504-0002',
  customer_name: 'MediaTek',
  wafer_quantity: 300,
  requested_delivery_date: '2026-06-05',
  scheduled_production_date: null,
  expected_delivery_date: null,
  status: 'pending',
  assigned_to: null,
  created_by: 'user-id',
  notes: null,
  version_id: 1,
  created_at: '2026-05-04T08:00:00Z',
  updated_at: '2026-05-04T08:00:00Z',
  pinned_production_date: null,
  is_pinned: false,
  is_processing_locked: false,
};

const secondPendingOrder: Order = {
  ...pendingOrder,
  id: '33333333-3333-4333-8333-333333333333',
  order_number: 'ORD-20260504-0003',
  customer_name: 'UMC',
};

const scheduledOrderDetail: Order = {
  ...pendingOrder,
  id: scheduledOrder.id,
  order_number: scheduledOrder.order_number,
  customer_name: scheduledOrder.customer_name,
  wafer_quantity: scheduledOrder.wafer_quantity,
  requested_delivery_date: scheduledOrder.requested_delivery_date,
  scheduled_production_date: scheduledOrder.scheduled_production_date,
  expected_delivery_date: scheduledOrder.expected_delivery_date,
  status: 'scheduled',
};

describe('OrdersCalendarDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRole.value = 'scheduler';
    mockScheduleResult.data = [scheduledOrder];
    mockScheduleResult.isPending = false;
    mockScheduleResult.isError = false;
    mockOrders.data = { items: [pendingOrder], total: 1, page: 1, page_size: 100 };
    mockOrders.isPending = false;
    mockOrders.isError = false;
    mockScheduledOrders.data = { items: [scheduledOrderDetail], total: 1, page: 1, page_size: 100 };
    mockScheduledOrders.isPending = false;
    mockScheduledOrders.isFetching = false;
    mockScheduledOrders.isSuccess = true;
    mockScheduledOrders.isError = false;
    mockPinSchedule.isPending = false;
    mockPinMutate.mockImplementation((_payload: unknown, options?: PinMutationOptions) => {
      options?.onSuccess?.();
    });
  });

  it('renders scheduled orders on their expected delivery date', async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole('button', { name: /2026-05-10/ }));

    const selectedPanel = screen.getByText('2026-05-10 完成訂單').closest('div');
    expect(selectedPanel).not.toBeNull();
    expect(within(selectedPanel as HTMLElement).getByText('ORD-20260504-0001')).toBeInTheDocument();
    expect(within(selectedPanel as HTMLElement).getByText(/TSMC/)).toBeInTheDocument();
  });

  it('renders pending orders without expected delivery date in the unscheduled panel', () => {
    renderDialog();

    expect(screen.getByText('未排程訂單')).toBeInTheDocument();
    expect(screen.getByText('ORD-20260504-0002')).toBeInTheDocument();
    expect(screen.getByText(/MediaTek/)).toBeInTheDocument();
  });

  it('can navigate between months', async () => {
    const user = userEvent.setup();
    renderDialog();

    const initialLabel = screen.getByRole('heading', { name: /年.*月/ }).textContent;
    await user.click(screen.getByRole('button', { name: '下一月' }));

    expect(screen.getByRole('heading', { name: /年.*月/ }).textContent).not.toBe(initialLabel);
  });

  it('shows an error state for viewer role', () => {
    mockRole.value = 'viewer';
    renderDialog();

    expect(screen.getByText('無法載入排程日曆，請確認帳號權限或稍後再試。')).toBeInTheDocument();
  });
  it('confirms and queues a pin attempt when a pending order is dropped onto a date', async () => {
    const user = userEvent.setup();
    renderDialog();

    const dragData = new Map<string, string>();
    const dataTransfer = {
      effectAllowed: '',
      dropEffect: '',
      setData: vi.fn((type: string, value: string) => dragData.set(type, value)),
      getData: vi.fn((type: string) => dragData.get(type) ?? ''),
    } as unknown as DataTransfer;

    fireEvent.dragStart(screen.getByText('ORD-20260504-0002'), { dataTransfer });
    fireEvent.drop(screen.getByRole('button', { name: /2026-05-10/ }), { dataTransfer });

    expect(screen.getByText('待送出的排程變更')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '送出嘗試' }));

    const [payload] = mockPinMutate.mock.calls[0] as [
      { targets: { order: { id: string }; targetDate: string }[] },
      unknown,
    ];
    expect(payload.targets.map((target) => target.order.id)).toEqual([pendingOrder.id]);
    expect(payload.targets.map((target) => target.targetDate)).toEqual(['2026-05-10']);
  });

  it('keeps selected order order when queueing a multi-order pin attempt', async () => {
    const user = userEvent.setup();
    mockOrders.data = {
      items: [pendingOrder, secondPendingOrder],
      total: 2,
      page: 1,
      page_size: 100,
    };
    renderDialog();

    await user.click(screen.getByRole('checkbox', { name: 'Select ORD-20260504-0002' }));
    await user.click(screen.getByRole('checkbox', { name: 'Select ORD-20260504-0003' }));

    const dragData = new Map<string, string>();
    const dataTransfer = {
      effectAllowed: '',
      dropEffect: '',
      setData: vi.fn((type: string, value: string) => dragData.set(type, value)),
      getData: vi.fn((type: string) => dragData.get(type) ?? ''),
    } as unknown as DataTransfer;

    fireEvent.dragStart(screen.getByText('ORD-20260504-0002'), { dataTransfer });
    fireEvent.drop(screen.getByRole('button', { name: /2026-05-10/ }), { dataTransfer });
    await user.click(screen.getByRole('button', { name: '送出嘗試' }));

    const [payload] = mockPinMutate.mock.calls[0] as [
      { targets: { order: { id: string }; targetDate: string }[] },
      unknown,
    ];
    expect(payload.targets.map((target) => target.order.id)).toEqual([
      pendingOrder.id,
      secondPendingOrder.id,
    ]);
    expect(payload.targets.map((target) => target.targetDate)).toEqual([
      '2026-05-10',
      '2026-05-10',
    ]);
  });

  it('queues separately dropped orders with different target dates in one compound', async () => {
    const user = userEvent.setup();
    mockOrders.data = {
      items: [pendingOrder, secondPendingOrder],
      total: 2,
      page: 1,
      page_size: 100,
    };
    renderDialog();

    const firstDragData = new Map<string, string>();
    const firstDataTransfer = {
      effectAllowed: '',
      dropEffect: '',
      setData: vi.fn((type: string, value: string) => firstDragData.set(type, value)),
      getData: vi.fn((type: string) => firstDragData.get(type) ?? ''),
    } as unknown as DataTransfer;
    fireEvent.dragStart(screen.getByText('ORD-20260504-0002'), {
      dataTransfer: firstDataTransfer,
    });
    fireEvent.drop(screen.getByRole('button', { name: /2026-05-10/ }), {
      dataTransfer: firstDataTransfer,
    });

    const secondDragData = new Map<string, string>();
    const secondDataTransfer = {
      effectAllowed: '',
      dropEffect: '',
      setData: vi.fn((type: string, value: string) => secondDragData.set(type, value)),
      getData: vi.fn((type: string) => secondDragData.get(type) ?? ''),
    } as unknown as DataTransfer;
    fireEvent.dragStart(screen.getByText('ORD-20260504-0003'), {
      dataTransfer: secondDataTransfer,
    });
    fireEvent.drop(screen.getByRole('button', { name: /2026-05-11/ }), {
      dataTransfer: secondDataTransfer,
    });

    await user.click(screen.getByRole('button', { name: '送出嘗試' }));

    const [payload] = mockPinMutate.mock.calls[0] as [
      { targets: { order: { id: string }; targetDate: string }[] },
      unknown,
    ];
    expect(payload.targets.map((target) => target.order.id)).toEqual([
      pendingOrder.id,
      secondPendingOrder.id,
    ]);
    expect(payload.targets.map((target) => target.targetDate)).toEqual([
      '2026-05-10',
      '2026-05-11',
    ]);
  });
});
