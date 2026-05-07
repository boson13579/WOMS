/**
 * OrdersPage — page composition tests.
 *
 * [RED]   tests written first to verify page-level composition behaviour
 * [GREEN] OrdersPage.tsx passes
 *
 * Strategy: child components (OrderFilters / OrderTable / OrderModal) are
 * mocked so tests focus on page-level state management and callback wiring,
 * not on details already covered by child component tests.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Order } from '../types';
import { OrdersPage } from './OrdersPage';

// ---------------------------------------------------------------------------
// Mock react-router-dom (keep original module, override useNavigate only)
// ---------------------------------------------------------------------------

const mockNavigate = vi.fn();

vi.mock('react-router-dom', async (importOriginal) => {
  const mod = await importOriginal<typeof import('react-router-dom')>();
  return { ...mod, useNavigate: () => mockNavigate };
});

// ---------------------------------------------------------------------------
// Mock useAuthStore
// ---------------------------------------------------------------------------

const mockLogout = vi.fn();

vi.mock('@/features/auth/stores/authStore', () => ({
  useAuthStore: () => ({ user: { username: 'alice' }, logout: mockLogout }),
}));

// ---------------------------------------------------------------------------
// Mock API / WS hook
// ---------------------------------------------------------------------------

const mockTriggerMutate = vi.fn();

vi.mock('../api/orders', () => ({
  useTriggerSchedule: () => ({ mutate: mockTriggerMutate }),
}));

vi.mock('../hooks/useScheduleWs', () => ({
  useScheduleWs: vi.fn(),
}));

// ---------------------------------------------------------------------------
// Mock child components
// ---------------------------------------------------------------------------

vi.mock('./OrderFilters', () => ({
  OrderFilters: () => <div data-testid="order-filters" />,
}));

// exposes buttons that trigger onEdit / onSchedule so the page's handlers can be tested
vi.mock('./OrderTable', () => ({
  OrderTable: ({
    onEdit,
    onSchedule,
  }: {
    onEdit: (o: Order) => void;
    onSchedule: (id: string) => void;
  }) => (
    <div data-testid="order-table">
      <button
        onClick={() =>
          onEdit({
            id: 'order-id-0001',
            order_number: 'ORD-20260504-0001',
            customer_name: 'TSMC',
            wafer_quantity: 500,
            requested_delivery_date: '2026-06-01',
            scheduled_production_date: null,
            expected_delivery_date: null,
            status: 'pending',
            assigned_to: null,
            created_by: 'user-id-0001',
            notes: null,
            version_id: 1,
            created_at: '2026-05-04T08:00:00Z',
            updated_at: '2026-05-04T08:00:00Z',
          })
        }
      >
        table-edit
      </button>
      <button onClick={() => onSchedule('test-order-id')}>table-schedule</button>
    </div>
  ),
}));

// exposes data-open / data-order attributes so tests can assert prop changes
vi.mock('./OrderModal', () => ({
  OrderModal: ({
    open,
    order,
    onClose,
  }: {
    open: boolean;
    order: Order | undefined;
    onClose: () => void;
  }) => (
    <div
      data-testid="order-modal"
      data-open={String(open)}
      data-order={order?.id ?? 'none'}
    >
      <button onClick={onClose}>modal-close</button>
    </div>
  ),
}));

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderPage(): void {
  render(
    <MemoryRouter>
      <OrdersPage />
    </MemoryRouter>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('OrdersPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the page heading', () => {
    renderPage();

    expect(
      screen.getByRole('heading', { name: 'Smart Order Management', level: 1 }),
    ).toBeInTheDocument();
  });

  it('renders the logged-in username', () => {
    renderPage();

    expect(screen.getByText('alice')).toBeInTheDocument();
  });

  it('renders the order list sub-heading', () => {
    renderPage();

    expect(screen.getByRole('heading', { name: '訂單列表', level: 2 })).toBeInTheDocument();
  });

  it('renders all three child components', () => {
    renderPage();

    expect(screen.getByTestId('order-filters')).toBeInTheDocument();
    expect(screen.getByTestId('order-table')).toBeInTheDocument();
    expect(screen.getByTestId('order-modal')).toBeInTheDocument();
  });

  it('modal is closed on initial render', () => {
    renderPage();

    expect(screen.getByTestId('order-modal')).toHaveAttribute('data-open', 'false');
  });

  it('opens the modal with no order when "新增訂單" is clicked', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('button', { name: /新增訂單/ }));

    const modal = screen.getByTestId('order-modal');
    expect(modal).toHaveAttribute('data-open', 'true');
    expect(modal).toHaveAttribute('data-order', 'none');
  });

  it('opens the modal with the order when OrderTable fires onEdit', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('button', { name: 'table-edit' }));

    const modal = screen.getByTestId('order-modal');
    expect(modal).toHaveAttribute('data-open', 'true');
    expect(modal).toHaveAttribute('data-order', 'order-id-0001');
  });

  it('closes the modal when onClose is called', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('button', { name: /新增訂單/ }));
    expect(screen.getByTestId('order-modal')).toHaveAttribute('data-open', 'true');

    await user.click(screen.getByRole('button', { name: 'modal-close' }));
    expect(screen.getByTestId('order-modal')).toHaveAttribute('data-open', 'false');
  });

  it('calls triggerSchedule.mutate(orderId) when OrderTable fires onSchedule', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('button', { name: 'table-schedule' }));

    expect(mockTriggerMutate).toHaveBeenCalledWith('test-order-id', expect.anything());
  });

  it('calls logout() and navigates to /login when the logout button is clicked', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('button', { name: /登出/ }));

    expect(mockLogout).toHaveBeenCalledOnce();
    expect(mockNavigate).toHaveBeenCalledWith('/login');
  });
});