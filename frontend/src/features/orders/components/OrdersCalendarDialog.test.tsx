import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ScheduleCapacity } from '../api/scheduleCapacity';
import type { Order, ScheduleResult } from '../types';

import { OrdersCalendarDialog } from './OrdersCalendarDialog';

const mockScheduleResult = {
  data: [] as ScheduleResult[],
  isPending: false,
  isError: false,
};

const mockScheduleCapacity = {
  data: {
    base_date: '2026-05-09',
    daily_capacity: 2500,
    entries: [
      { date: '2026-05-09', used: 1000, remaining: 1500 },
      { date: '2026-05-10', used: 1500, remaining: 1000 },
      { date: '2026-05-11', used: 2500, remaining: 0 },
    ],
  } as ScheduleCapacity | undefined,
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

vi.mock('../api/scheduleCapacity', () => ({
  toDailyCapacity: (capacity: {
    daily_capacity: number;
    entries: { date: string; used: number; remaining: number }[];
  }) =>
    capacity.entries.map((entry) => ({
      date: entry.date,
      used: entry.used,
      remaining: entry.remaining,
      dailyCapacity: capacity.daily_capacity,
    })),
  useScheduleCapacity: () => mockScheduleCapacity,
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

function setServerBaseDate(baseDate: string): void {
  if (!mockScheduleCapacity.data) {
    throw new Error('mockScheduleCapacity.data must be defined before setting base_date');
  }
  mockScheduleCapacity.data = { ...mockScheduleCapacity.data, base_date: baseDate };
}

const inProductionOrder: ScheduleResult = {
  id: '11111111-1111-4111-8111-111111111111',
  order_number: 'ORD-20260504-0001',
  customer_name: 'TSMC',
  wafer_quantity: 500,
  requested_delivery_date: '2026-06-01',
  scheduled_production_date: '2026-05-09',
  expected_delivery_date: '2026-05-10',
  status: 'in_production',
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

const inProductionOrderDetail: Order = {
  ...pendingOrder,
  id: inProductionOrder.id,
  order_number: inProductionOrder.order_number,
  customer_name: inProductionOrder.customer_name,
  wafer_quantity: inProductionOrder.wafer_quantity,
  requested_delivery_date: inProductionOrder.requested_delivery_date,
  scheduled_production_date: inProductionOrder.scheduled_production_date,
  expected_delivery_date: inProductionOrder.expected_delivery_date,
  status: 'in_production',
  is_pinned: true,
};

const scheduledOrderDetail: Order = {
  ...pendingOrder,
  id: '55555555-5555-4555-8555-555555555555',
  order_number: 'ORD-20260504-0005',
  customer_name: 'Nanya',
  wafer_quantity: 800,
  requested_delivery_date: '2026-05-12',
  scheduled_production_date: '2026-05-09',
  expected_delivery_date: '2026-05-10',
  status: 'scheduled',
  pinned_production_date: null,
  is_pinned: false,
};

const scheduledOrderResult: ScheduleResult = {
  id: scheduledOrderDetail.id,
  order_number: scheduledOrderDetail.order_number,
  customer_name: scheduledOrderDetail.customer_name,
  wafer_quantity: scheduledOrderDetail.wafer_quantity,
  requested_delivery_date: scheduledOrderDetail.requested_delivery_date,
  scheduled_production_date: scheduledOrderDetail.scheduled_production_date,
  expected_delivery_date: scheduledOrderDetail.expected_delivery_date,
  status: 'scheduled',
  daily_breakdown: [{ date: '2026-05-09', quantity: 800 }],
};

const splitInProductionOrder: ScheduleResult = {
  ...inProductionOrder,
  id: '44444444-4444-4444-8444-444444444444',
  order_number: 'ORD-20260504-0004',
  customer_name: 'ASE',
  wafer_quantity: 2500,
  status: 'in_production',
  daily_breakdown: [
    { date: '2026-05-10', quantity: 1000 },
    { date: '2026-05-11', quantity: 1500 },
  ],
};

describe('OrdersCalendarDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-05-09T00:00:00Z'));

    mockRole.value = 'scheduler';
    mockScheduleCapacity.data = {
      base_date: '2026-05-09',
      daily_capacity: 2500,
      entries: [
        { date: '2026-05-09', used: 1000, remaining: 1500 },
        { date: '2026-05-10', used: 1500, remaining: 1000 },
        { date: '2026-05-11', used: 2500, remaining: 0 },
      ],
    };
    mockScheduleCapacity.isPending = false;
    mockScheduleCapacity.isError = false;
    mockScheduleResult.data = [inProductionOrder];
    mockScheduleResult.isPending = false;
    mockScheduleResult.isError = false;
    mockOrders.data = { items: [pendingOrder], total: 1, page: 1, page_size: 100 };
    mockOrders.isPending = false;
    mockOrders.isError = false;
    mockScheduledOrders.data = {
      items: [inProductionOrderDetail],
      total: 1,
      page: 1,
      page_size: 100,
    };
    mockScheduledOrders.isPending = false;
    mockScheduledOrders.isFetching = false;
    mockScheduledOrders.isSuccess = true;
    mockScheduledOrders.isError = false;
    mockPinSchedule.isPending = false;
    mockPinMutate.mockImplementation((_payload: unknown, options?: PinMutationOptions) => {
      options?.onSuccess?.();
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders scheduled orders on their production date', async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole('button', { name: /2026-05-09/ }));

    expect(screen.getByText('2026-05-09 生產訂單')).toBeInTheDocument();
    expect(screen.getAllByText('ORD-20260504-0001').length).toBeGreaterThan(0);
    expect(screen.getByText(/TSMC/)).toBeInTheDocument();
    expect(screen.getAllByText('需求日 2026-06-01').length).toBeGreaterThan(0);
    expect(screen.getByText(/今日 500/)).toBeInTheDocument();
    expect(screen.getByText('生產中')).toBeInTheDocument();
    expect(screen.getByText('剩餘 1,500')).toBeInTheDocument();
  });

  it('pins a scheduled order from the right panel', async () => {
    const user = userEvent.setup();
    mockScheduleResult.data = [scheduledOrderResult];
    mockScheduledOrders.data = {
      items: [scheduledOrderDetail],
      total: 1,
      page: 1,
      page_size: 100,
    };
    renderDialog();

    await user.click(screen.getByRole('button', { name: /2026-05-09/ }));
    await user.click(screen.getByRole('button', { name: '固定到此日' }));

    const [payload] = mockPinMutate.mock.calls[0] as [
      { targets: { order: { id: string }; targetDate: string | null }[] },
      unknown,
    ];
    expect(payload.targets[0].order.id).toBe(scheduledOrderDetail.id);
    expect(payload.targets[0].targetDate).toBe('2026-05-09');
  });

  it('unpins a scheduled order from the right panel', async () => {
    const user = userEvent.setup();
    mockScheduleResult.data = [scheduledOrderResult];
    mockScheduledOrders.data = {
      items: [
        {
          ...scheduledOrderDetail,
          is_pinned: true,
          pinned_production_date: '2026-05-09',
        },
      ],
      total: 1,
      page: 1,
      page_size: 100,
    };
    renderDialog();

    await user.click(screen.getByRole('button', { name: /2026-05-09/ }));
    await user.click(screen.getByRole('button', { name: '取消固定' }));

    const [payload] = mockPinMutate.mock.calls[0] as [
      { targets: { order: { id: string }; targetDate: string | null }[] },
      unknown,
    ];
    expect(payload.targets[0].order.id).toBe(scheduledOrderDetail.id);
    expect(payload.targets[0].targetDate).toBeNull();
  });

  it('renders pending orders without expected delivery date in the unscheduled panel', () => {
    renderDialog();

    expect(screen.getByText('未排程訂單')).toBeInTheDocument();
    expect(screen.getByText('ORD-20260504-0002')).toBeInTheDocument();
    expect(screen.getByText(/MediaTek/)).toBeInTheDocument();
  });

  it('renders calendar rows while server base_date is loading', () => {
    mockScheduleCapacity.data = undefined;
    mockScheduleCapacity.isPending = true;

    renderDialog();

    expect(screen.getByText('載入日曆中...')).toBeInTheDocument();
    expect(screen.getByText('ORD-20260504-0001')).toBeInTheDocument();
  });

  it('shows split production progress across production dates', async () => {
    const user = userEvent.setup();
    mockScheduleResult.data = [splitInProductionOrder];
    setServerBaseDate('2026-05-10');
    renderDialog();

    await user.click(screen.getByRole('button', { name: /2026-05-10/ }));
    expect(screen.getByText(/今日 1,000/)).toBeInTheDocument();
    expect(screen.getByText(/累計 1,000 \/ 2,500/)).toBeInTheDocument();
    expect(screen.getByText('生產中')).toBeInTheDocument();
    expect(screen.getByText('剩餘 1,000')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /2026-05-11/ }));
    expect(screen.getByText(/今日 1,500/)).toBeInTheDocument();
    expect(screen.getByText(/累計 2,500 \/ 2,500/)).toBeInTheDocument();
    expect(screen.getByText('生產中')).toBeInTheDocument();
    expect(screen.getByText('剩餘 0')).toBeInTheDocument();
  });

  it('shows split production as completed when status is completed', async () => {
    const user = userEvent.setup();
    mockScheduleResult.data = [
      {
        ...splitInProductionOrder,
        status: 'completed',
      },
    ];
    setServerBaseDate('2026-05-12');
    renderDialog();

    await user.click(screen.getByRole('button', { name: /2026-05-11/ }));
    expect(screen.getByText('已完成')).toBeInTheDocument();
  });

  it('uses order status instead of the client clock for production state', async () => {
    const user = userEvent.setup();
    vi.setSystemTime(new Date('2026-05-12T00:00:00Z'));
    setServerBaseDate('2026-05-10');
    mockScheduleResult.data = [splitInProductionOrder];
    renderDialog();

    await user.click(screen.getByRole('button', { name: /2026-05-11/ }));

    expect(screen.getByText('生產中')).toBeInTheDocument();
  });

  it('displays a padlock icon for production-active or pinned orders', async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole('button', { name: /2026-05-09/ }));
    expect(screen.getAllByLabelText('已鎖定').length).toBeGreaterThan(0);
  });

  it('does not drag a scheduled calendar item when the canonical order record is not loaded', () => {
    mockScheduledOrders.data = { items: [], total: 0, page: 1, page_size: 100 };
    renderDialog();

    const dragData = new Map<string, string>();
    const setData = vi.fn((type: string, value: string) => dragData.set(type, value));
    const dataTransfer = {
      effectAllowed: '',
      dropEffect: '',
      setData,
      getData: vi.fn((type: string) => dragData.get(type) ?? ''),
    } as unknown as DataTransfer;

    fireEvent.dragStart(screen.getByText('ORD-20260504-0001'), { dataTransfer });

    expect(setData).not.toHaveBeenCalled();
    expect(dataTransfer.effectAllowed).toBe('');
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

  it('highlights today\'s date with amber styling when not selected', async () => {
    const user = userEvent.setup();
    renderDialog();
    
    // Get today's date and a different date
    const today = new Date();
    const todayDateString = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
    
    // Get tomorrow's date to click on
    const tomorrow = new Date(today);
    tomorrow.setDate(tomorrow.getDate() + 1);
    const tomorrowDateString = `${tomorrow.getFullYear()}-${String(tomorrow.getMonth() + 1).padStart(2, '0')}-${String(tomorrow.getDate()).padStart(2, '0')}`;
    
    // Click tomorrow to deselect today
    const tomorrowButton = screen.getByRole('button', { name: new RegExp(`^${tomorrowDateString}`) });
    await user.click(tomorrowButton);
    
    // Find today's button
    const todayButton = screen.getByRole('button', { name: new RegExp(`^${todayDateString}`) });
    
    // When today is not selected, it should have amber styling
    expect(todayButton).toHaveClass('bg-amber-50');
    expect(todayButton).toHaveClass('ring-amber-400');
    expect(todayButton).toHaveClass('dark:bg-amber-950/30');
  });

  it('highlights today\'s date with blue ring when selected', async () => {
    const user = userEvent.setup();
    renderDialog();
    
    // Get today's date
    const today = new Date();
    const todayDateString = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
    
    // Find and click today's date to ensure it's selected
    const todayButton = screen.getByRole('button', { name: new RegExp(`^${todayDateString}`) });
    await user.click(todayButton);
    
    // When today is selected, it should have amber background with blue ring
    expect(todayButton).toHaveClass('bg-amber-50');
    expect(todayButton).toHaveClass('ring-sky-500');
    expect(todayButton).toHaveClass('dark:bg-amber-950/30');
  });
});
