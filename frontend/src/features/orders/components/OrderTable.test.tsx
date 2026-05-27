/**
 * OrderTable — paginated list, pagination, delete, cancel.
 *
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Order, OrderListResponse } from '../types';

import { OrderTable } from './OrderTable';

// ---------------------------------------------------------------------------
// Mock @/lib/auth — controls canWrite / canSchedule / role per test
// ---------------------------------------------------------------------------

let mockCanWrite = true;
let mockRole = 'scheduler';
const mockCurrentUserId = 'test-user-id';

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => ({ username: 'test', role: mockRole }),
  useCanWrite: () => mockCanWrite,
  useCurrentRole: () => mockRole,
  useCurrentUserId: () => mockCurrentUserId,
}));

vi.mock('@/features/users/api/useUsernames', () => ({
  useUsernames: () => ({ data: undefined }),
}));

// ---------------------------------------------------------------------------
// Mock hooks
// ---------------------------------------------------------------------------

const mockSetPage = vi.fn();
const mockSetSort = vi.fn();
const mockDeleteMutate = vi.fn();
const mockCancelMutate = vi.fn();

const mockStore = {
  status: null as string | null,
  search: '',
  assignedTo: [] as string[],
  createdBy: [] as string[],
  page: 1,
  sortBy: 'order_number' as string,
  sortOrder: 'asc' as 'asc' | 'desc',
  setPage: mockSetPage,
  setSort: mockSetSort,
};

vi.mock('../stores/orderStore', () => ({
  useOrderStore: () => mockStore,
}));

// return values are overridden per test to exercise different query states
const mockUseOrders = vi.fn();
const mockUseDeleteOrder = vi.fn();
const mockUseCancelOrder = vi.fn();

vi.mock('../api/orders', () => ({
  // eslint-disable-next-line @typescript-eslint/no-unsafe-return
  useOrders: (...args: unknown[]) => mockUseOrders(...args),
  // eslint-disable-next-line @typescript-eslint/no-unsafe-return
  useDeleteOrder: () => mockUseDeleteOrder(),
  // eslint-disable-next-line @typescript-eslint/no-unsafe-return
  useCancelOrder: () => mockUseCancelOrder(),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeOrder(overrides: Partial<Order> = {}): Order {
  return {
    id: '11111111-0000-0000-0000-000000000001',
    order_number: 'ORD-20260504-0001',
    customer_name: 'TSMC',
    wafer_quantity: 500,
    requested_delivery_date: '2026-06-01',
    scheduled_production_date: null,
    expected_delivery_date: null,
    status: 'pending',
    assigned_to: null,
    created_by: 'aaaaaaaa-0000-0000-0000-000000000001',
    notes: null,
    version_id: 1,
    created_at: '2026-05-04T08:00:00Z',
    updated_at: '2026-05-04T08:00:00Z',
    pinned_production_date: null,
    is_pinned: false,
    is_processing_locked: false,
    ...overrides,
  };
}

function makeList(items: Order[]): OrderListResponse {
  return { items, total: items.length, page: 1, page_size: 20 };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('OrderTable', () => {
  const onEdit = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    mockCanWrite = true;
    mockRole = 'scheduler';
    mockStore.sortBy = 'order_number';
    mockStore.sortOrder = 'asc';
    mockUseDeleteOrder.mockReturnValue({ mutate: mockDeleteMutate, isPending: false });
    mockUseCancelOrder.mockReturnValue({ mutate: mockCancelMutate, isPending: false });
  });

  it('shows a loading spinner while data is pending', () => {
    mockUseOrders.mockReturnValue({ isPending: true, isError: false, data: undefined });

    render(<OrderTable onEdit={onEdit} />);

    expect(screen.getByText(/載入中/)).toBeInTheDocument();
  });

  it('shows an error message when the API fails', () => {
    mockUseOrders.mockReturnValue({ isPending: false, isError: true, data: undefined });

    render(<OrderTable onEdit={onEdit} />);

    expect(screen.getByText(/載入失敗/)).toBeInTheDocument();
  });

  it('shows an empty-state message when there are no results', () => {
    mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([]) });

    render(<OrderTable onEdit={onEdit} />);

    expect(screen.getByText(/沒有符合條件的訂單/)).toBeInTheDocument();
  });

  it('renders order rows when data is available', () => {
    const order = makeOrder({ customer_name: 'TSMC', order_number: 'ORD-20260504-0001' });
    mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

    render(<OrderTable onEdit={onEdit} />);

    expect(screen.getByText('TSMC')).toBeInTheDocument();
    expect(screen.getByText('ORD-20260504-0001')).toBeInTheDocument();
    expect(screen.getByText('500')).toBeInTheDocument(); // wafer_quantity
  });

  it('renders the status badge', () => {
    const order = makeOrder({ status: 'scheduled' });
    mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

    render(<OrderTable onEdit={onEdit} />);

    expect(screen.getByText('已排程')).toBeInTheDocument();
  });

  it('calls onEdit(order) when the edit button is clicked', async () => {
    const user = userEvent.setup();
    const order = makeOrder();
    mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

    render(<OrderTable onEdit={onEdit} />);
    await user.click(screen.getByTitle('編輯'));

    expect(onEdit).toHaveBeenCalledWith(order);
  });

  it('does not call deleteMutation.mutate when the confirm dialog is cancelled', async () => {
    const user = userEvent.setup();
    vi.spyOn(window, 'confirm').mockReturnValue(false); // simulates user clicking "Cancel"

    const order = makeOrder();
    mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

    render(<OrderTable onEdit={onEdit} />);
    await user.click(screen.getByTitle('刪除'));

    expect(mockDeleteMutate).not.toHaveBeenCalled();
  });

  it('calls deleteMutation.mutate(order.id) when the confirm dialog is accepted', async () => {
    const user = userEvent.setup();
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    const order = makeOrder({ id: 'delete-me-id' });
    mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

    render(<OrderTable onEdit={onEdit} />);
    await user.click(screen.getByTitle('刪除'));

    expect(mockDeleteMutate).toHaveBeenCalledWith(
      'delete-me-id',
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });

  it('calls setSort with the column field when a sortable header is clicked', async () => {
    const user = userEvent.setup();
    const order = makeOrder();
    mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

    render(<OrderTable onEdit={onEdit} />);
    await user.click(screen.getByText('客戶'));

    expect(mockSetSort).toHaveBeenCalledWith('customer_name');
  });

  it('calls setSort again to toggle sort order when the active column header is clicked', async () => {
    const user = userEvent.setup();
    mockStore.sortBy = 'customer_name';
    mockStore.sortOrder = 'asc';
    const order = makeOrder();
    mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

    render(<OrderTable onEdit={onEdit} />);
    await user.click(screen.getByText('客戶'));

    expect(mockSetSort).toHaveBeenCalledWith('customer_name');
  });

  describe('role-based rendering', () => {
    it('scheduler — shows edit and delete buttons', () => {
      mockCanWrite = true;
      mockRole = 'scheduler';
      const order = makeOrder();
      mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

      render(<OrderTable onEdit={onEdit} />);

      expect(screen.getByTitle('編輯')).toBeInTheDocument();
      expect(screen.getByTitle('刪除')).toBeInTheDocument();
    });

    it('root — shows edit and delete buttons', () => {
      mockCanWrite = true;
      mockRole = 'root';
      const order = makeOrder();
      mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

      render(<OrderTable onEdit={onEdit} />);

      expect(screen.getByTitle('編輯')).toBeInTheDocument();
      expect(screen.getByTitle('刪除')).toBeInTheDocument();
    });

    it("order_manager — hides edit and delete for another user's order", () => {
      mockCanWrite = true;
      mockRole = 'order_manager';
      // mockCurrentUserId ('test-user-id') !== order.created_by ('aaaaaaaa-...') → canEditOrder=false
      const order = makeOrder();
      mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

      render(<OrderTable onEdit={onEdit} />);

      expect(screen.queryByTitle('編輯')).not.toBeInTheDocument();
      expect(screen.queryByTitle('刪除')).not.toBeInTheDocument();
    });
  });

  describe('is_processing_locked', () => {
    it('shows a lock icon next to the order number when locked', () => {
      const order = makeOrder({ is_processing_locked: true });
      mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

      render(<OrderTable onEdit={onEdit} />);

      expect(screen.getByLabelText('排程處理中')).toBeInTheDocument();
    });

    it('does not show a lock icon when not locked', () => {
      const order = makeOrder({ is_processing_locked: false });
      mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

      render(<OrderTable onEdit={onEdit} />);

      expect(screen.queryByLabelText('排程處理中')).not.toBeInTheDocument();
    });

    it('disables edit, cancel and delete buttons when locked', () => {
      // Use status=scheduled so all three buttons fall through to the
      // locked-title branch (cancel has a stricter "scheduled-only" gate).
      const order = makeOrder({ status: 'scheduled', is_processing_locked: true });
      mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

      render(<OrderTable onEdit={onEdit} />);

      const lockedButtons = screen.getAllByTitle('排程處理中，請稍候');
      expect(lockedButtons).toHaveLength(3);
      lockedButtons.forEach((btn) => {
        expect(btn).toBeDisabled();
      });
    });
  });

  describe('cancel action', () => {
    it('does not call cancelMutation.mutate when the confirm dialog is cancelled', async () => {
      const user = userEvent.setup();
      vi.spyOn(window, 'confirm').mockReturnValue(false);

      const order = makeOrder({ status: 'scheduled' });
      mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

      render(<OrderTable onEdit={onEdit} />);
      await user.click(screen.getByTitle('取消訂單'));

      expect(mockCancelMutate).not.toHaveBeenCalled();
    });

    it('calls cancelMutation.mutate(order.id) when the confirm dialog is accepted', async () => {
      const user = userEvent.setup();
      vi.spyOn(window, 'confirm').mockReturnValue(true);

      const order = makeOrder({ id: 'cancel-me-id', status: 'scheduled' });
      mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

      render(<OrderTable onEdit={onEdit} />);
      await user.click(screen.getByTitle('取消訂單'));

      expect(mockCancelMutate).toHaveBeenCalledWith(
        'cancel-me-id',
        // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
        expect.objectContaining({ onSuccess: expect.any(Function), onError: expect.any(Function) }),
      );
    });

    it('disables the cancel button for non-scheduled orders (pending)', () => {
      const order = makeOrder({ status: 'pending' });
      mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

      render(<OrderTable onEdit={onEdit} />);

      expect(screen.getByTitle('只有已排程的訂單可以取消')).toBeDisabled();
    });
  });

  describe('immutable statuses (mirror backend PATCH/cancel 4xx)', () => {
    it.each([
      ['cancelled', '訂單已取消，無法編輯或取消'],
      ['completed', '訂單已完成，無法編輯或取消'],
      ['in_production', '訂單生產中，無法編輯或取消'],
    ] as const)('disables edit + cancel when status=%s', (orderStatus, expectedTitle) => {
      const order = makeOrder({ status: orderStatus });
      mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

      render(<OrderTable onEdit={onEdit} />);

      const blocked = screen.getAllByTitle(expectedTitle);
      expect(blocked).toHaveLength(2); // edit + cancel
      blocked.forEach((btn) => {
        expect(btn).toBeDisabled();
      });
    });

    it.each(['cancelled', 'completed', 'in_production'] as const)(
      'leaves the delete button enabled when status=%s (backend has no status guard on DELETE)',
      (orderStatus) => {
        const order = makeOrder({ status: orderStatus });
        mockUseOrders.mockReturnValue({
          isPending: false,
          isError: false,
          data: makeList([order]),
        });

        render(<OrderTable onEdit={onEdit} />);

        expect(screen.getByTitle('刪除')).not.toBeDisabled();
      },
    );
  });

  it('does not render pagination when there is only one page', () => {
    const order = makeOrder();
    mockUseOrders.mockReturnValue({ isPending: false, isError: false, data: makeList([order]) });

    render(<OrderTable onEdit={onEdit} />);

    expect(screen.queryByRole('button', { name: /上一頁/ })).not.toBeInTheDocument();
  });
});
