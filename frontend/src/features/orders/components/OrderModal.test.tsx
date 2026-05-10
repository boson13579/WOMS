/**
 * OrderModal — create / edit form.
 *
 * [RED]   tests written first
 * [GREEN] OrderModal.tsx passes
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Order } from '../types';

import { OrderModal } from './OrderModal';

// ---------------------------------------------------------------------------
// Mock mutations
// ---------------------------------------------------------------------------

const mockCreateMutate = vi.fn();
const mockUpdateMutate = vi.fn();

vi.mock('../api/orders', () => ({
  useCreateOrder: () => ({ mutate: mockCreateMutate, isPending: false, isError: false }),
  useUpdateOrder: () => ({ mutate: mockUpdateMutate, isPending: false, isError: false }),
}));

vi.mock('@/features/auth/api/users', () => {
  // Must be a stable reference — if useUsers() returns a new array every render,
  // OrderModal's useEffect([order, reset, users]) re-runs infinitely and hangs.
  const stableUsers = [
    { id: 'uid-001', username: 'alice', email: 'alice@example.com' },
    { id: 'uid-002', username: 'bob', email: 'bob@example.com' },
  ];
  return { useUsers: () => stableUsers };
});

// Radix Dialog has animation timers that keep the test runner alive.
// Replace with a plain stub so tests exit cleanly.
vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ open, children }: { open: boolean; children: React.ReactNode }) =>
    open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
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
    notes: '測試備註',
    version_id: 1,
    created_at: '2026-05-04T08:00:00Z',
    updated_at: '2026-05-04T08:00:00Z',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('OrderModal', () => {
  const onClose = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  // --- create mode ---

  it('create mode: title shows "新增訂單"', () => {
    render(<OrderModal open order={undefined} onClose={onClose} />);

    expect(screen.getByRole('heading', { name: '新增訂單' })).toBeInTheDocument();
  });

  it('create mode: submit button shows "新增"', () => {
    render(<OrderModal open order={undefined} onClose={onClose} />);

    expect(screen.getByRole('button', { name: '新增' })).toBeInTheDocument();
  });

  it('create mode: shows validation errors when submitted empty', async () => {
    const user = userEvent.setup();
    render(<OrderModal open order={undefined} onClose={onClose} />);

    await user.click(screen.getByRole('button', { name: '新增' }));

    expect(screen.getByText(/請填寫客戶名稱/)).toBeInTheDocument();
    expect(screen.getByText(/請選擇要求交貨日/)).toBeInTheDocument();
    expect(mockCreateMutate).not.toHaveBeenCalled();
  });

  it('create mode: calls createMutation.mutate with form values', async () => {
    const user = userEvent.setup();
    render(<OrderModal open order={undefined} onClose={onClose} />);

    await user.clear(screen.getByLabelText(/客戶名稱/));
    await user.type(screen.getByLabelText(/客戶名稱/), 'Samsung');

    await user.clear(screen.getByLabelText(/晶圓數量/));
    await user.type(screen.getByLabelText(/晶圓數量/), '200');

    await user.type(screen.getByLabelText(/要求交貨日/), '2026-08-01');

    await user.type(screen.getByLabelText(/負責人/), 'alice@example.com');

    await user.click(screen.getByRole('button', { name: '新增' }));

    expect(mockCreateMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        customer_name: 'Samsung',
        wafer_quantity: 200,
        requested_delivery_date: '2026-08-01',
      }),
      expect.anything(),
    );
  });

  // --- edit mode ---

  it('edit mode: title shows "編輯訂單"', () => {
    render(<OrderModal open order={makeOrder()} onClose={onClose} />);

    expect(screen.getByRole('heading', { name: '編輯訂單' })).toBeInTheDocument();
  });

  it('edit mode: pre-fills customer name', () => {
    render(<OrderModal open order={makeOrder({ customer_name: 'Intel' })} onClose={onClose} />);

    expect(screen.getByDisplayValue('Intel')).toBeInTheDocument();
  });

  it('edit mode: pre-fills wafer quantity', () => {
    render(<OrderModal open order={makeOrder({ wafer_quantity: 1200 })} onClose={onClose} />);

    expect(screen.getByDisplayValue('1200')).toBeInTheDocument();
  });

  it('edit mode: passes version_id to updateMutation.mutate', async () => {
    const user = userEvent.setup();
    // assigned_to must match a mock user so the pre-filled email passes validation
    const order = makeOrder({ id: 'edit-id', version_id: 3, assigned_to: 'uid-001' });
    render(<OrderModal open order={order} onClose={onClose} />);

    await user.click(screen.getByRole('button', { name: '儲存' }));

    expect(mockUpdateMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'edit-id',
        // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
        payload: expect.objectContaining({ version_id: 3 }),
      }),
      expect.anything(),
    );
  });

  // --- shared ---

  it('calls onClose() when the cancel button is clicked', async () => {
    const user = userEvent.setup();
    render(<OrderModal open order={undefined} onClose={onClose} />);

    await user.click(screen.getByRole('button', { name: '取消' }));

    expect(onClose).toHaveBeenCalledOnce();
  });
});
