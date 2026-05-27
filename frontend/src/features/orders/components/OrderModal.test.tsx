/**
 * OrderModal — create / edit form.
 *
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/apiFetch';

import type { Order } from '../types';

import { OrderModal } from './OrderModal';

// ---------------------------------------------------------------------------
// Mock mutations — state is mutable so individual tests can set isError/error
// ---------------------------------------------------------------------------

const mockCreateMutate = vi.fn();
const mockUpdateMutate = vi.fn();

interface MutationState {
  mutate: ReturnType<typeof vi.fn>;
  isPending: boolean;
  isError: boolean;
  error: Error | null;
}

let createState: MutationState;
let updateState: MutationState;

// Spread the real module so ApiError keeps its class identity for instanceof checks.
vi.mock('../api/orders', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('../api/orders');
  return {
    ...actual,
    useCreateOrder: () => createState,
    useUpdateOrder: () => updateState,
  };
});

vi.mock('@/features/auth/api/users', () => {
  // Must be a stable reference — if the hook returns a new array every render,
  // OrderModal's useEffect([order, reset, users]) re-runs infinitely and hangs.
  const stableUsers = [
    { id: 'uid-001', username: 'alice', email: 'alice@example.com' },
    { id: 'uid-002', username: 'bob', email: 'bob@example.com' },
  ];
  return { useAssignableUsers: () => stableUsers };
});

// The modal derives its deadline window from the scheduler's authoritative
// base_date (UTC) + horizon via useScheduleCapacity — NOT the local clock.
// Mock a fixed base_date with a full 30-entry horizon so the window is
// deterministic and timezone-independent. Factory is self-contained (no outer
// refs) to satisfy vi.mock hoisting.
vi.mock('../api/scheduleCapacity', () => {
  const base = '2026-06-01';
  const addUTC = (n: number): string => {
    const d = new Date(`${base}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  };
  return {
    useScheduleCapacity: () => ({
      data: {
        base_date: base,
        daily_capacity: 200,
        entries: Array.from({ length: 30 }, (_, i) => ({
          date: addUTC(i),
          used: 0,
          remaining: 200,
        })),
      },
    }),
  };
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

// Mirrors the mocked scheduler base_date above. `fromBase(n)` returns the UTC
// Y-M-D string for base_date+n; the modal's valid window is [base+1, base+30].
const BASE_DATE = '2026-06-01';

function fromBase(days: number): string {
  const d = new Date(`${BASE_DATE}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

// Valid date well inside the horizon — used by every "happy path" test.
const FUTURE_DATE = fromBase(15);

function makeOrder(overrides: Partial<Order> = {}): Order {
  return {
    id: '11111111-0000-0000-0000-000000000001',
    order_number: 'ORD-TEST-0001',
    customer_name: 'TSMC',
    wafer_quantity: 500,
    requested_delivery_date: fromBase(10),
    scheduled_production_date: null,
    expected_delivery_date: null,
    status: 'pending',
    assigned_to: null,
    created_by: 'aaaaaaaa-0000-0000-0000-000000000001',
    notes: '測試備註',
    version_id: 1,
    created_at: '2026-01-01T08:00:00Z',
    updated_at: '2026-01-01T08:00:00Z',
    pinned_production_date: null,
    is_pinned: false,
    is_processing_locked: false,
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
    createState = { mutate: mockCreateMutate, isPending: false, isError: false, error: null };
    updateState = { mutate: mockUpdateMutate, isPending: false, isError: false, error: null };
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

    await user.type(screen.getByLabelText(/要求交貨日/), FUTURE_DATE);

    await user.type(screen.getByLabelText(/負責人/), 'alice@example.com');

    await user.click(screen.getByRole('button', { name: '新增' }));

    expect(mockCreateMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        customer_name: 'Samsung',
        wafer_quantity: 200,
        requested_delivery_date: FUTURE_DATE,
      }),
      expect.anything(),
    );
  });

  it('create mode: allows order managers to assign from the assignable users list', () => {
    render(<OrderModal open order={undefined} onClose={onClose} />);

    const assigneeInput = screen.getByLabelText(/負責人/);
    expect(assigneeInput).toBeEnabled();
    expect(assigneeInput).toHaveAttribute('list', 'users-datalist');
  });

  it('create mode: fails validation if responsible email is not in the assignable users list', async () => {
    const user = userEvent.setup();
    render(<OrderModal open order={undefined} onClose={onClose} />);

    await user.clear(screen.getByLabelText(/客戶名稱/));
    await user.type(screen.getByLabelText(/客戶名稱/), 'Samsung');

    await user.clear(screen.getByLabelText(/晶圓數量/));
    await user.type(screen.getByLabelText(/晶圓數量/), '200');

    await user.type(screen.getByLabelText(/要求交貨日/), FUTURE_DATE);

    await user.type(screen.getByLabelText(/負責人/), 'invalid-email@random.com');

    await user.click(screen.getByRole('button', { name: '新增' }));

    expect(screen.getByText('負責人必須是系統中現有的使用者')).toBeInTheDocument();
    expect(mockCreateMutate).not.toHaveBeenCalled();
  });

  it('create mode: a valid email from the list maps to its user id on submit', async () => {
    const user = userEvent.setup();
    render(<OrderModal open order={undefined} onClose={onClose} />);

    await user.clear(screen.getByLabelText(/客戶名稱/));
    await user.type(screen.getByLabelText(/客戶名稱/), 'Samsung');

    await user.clear(screen.getByLabelText(/晶圓數量/));
    await user.type(screen.getByLabelText(/晶圓數量/), '200');

    await user.type(screen.getByLabelText(/要求交貨日/), FUTURE_DATE);
    await user.type(screen.getByLabelText(/負責人/), 'bob@example.com');

    await user.click(screen.getByRole('button', { name: '新增' }));

    expect(mockCreateMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        customer_name: 'Samsung',
        assigned_to: 'uid-002',
      }),
      expect.anything(),
    );
  });

  // --- date validation ---

  it('create mode: sets min/max on the date input matching today+1 and today+30', () => {
    render(<OrderModal open order={undefined} onClose={onClose} />);

    const dateInput = screen.getByLabelText(/要求交貨日/);
    expect(dateInput).toHaveAttribute('min', fromBase(1));
    expect(dateInput).toHaveAttribute('max', fromBase(30));
  });

  it('create mode: rejects a delivery date that is today or earlier', async () => {
    const user = userEvent.setup();
    render(<OrderModal open order={undefined} onClose={onClose} />);

    await user.type(screen.getByLabelText(/客戶名稱/), 'Samsung');
    await user.clear(screen.getByLabelText(/晶圓數量/));
    await user.type(screen.getByLabelText(/晶圓數量/), '200');
    await user.type(screen.getByLabelText(/要求交貨日/), fromBase(0));

    await user.click(screen.getByRole('button', { name: '新增' }));

    expect(screen.getByText(/交貨日必須是明天之後/)).toBeInTheDocument();
    expect(mockCreateMutate).not.toHaveBeenCalled();
  });

  it('create mode: rejects a delivery date past the 30-day horizon', async () => {
    const user = userEvent.setup();
    render(<OrderModal open order={undefined} onClose={onClose} />);

    await user.type(screen.getByLabelText(/客戶名稱/), 'Samsung');
    await user.clear(screen.getByLabelText(/晶圓數量/));
    await user.type(screen.getByLabelText(/晶圓數量/), '200');
    await user.type(screen.getByLabelText(/要求交貨日/), fromBase(31));

    await user.click(screen.getByRole('button', { name: '新增' }));

    expect(screen.getByText(/超過 30 天排程範圍/)).toBeInTheDocument();
    expect(mockCreateMutate).not.toHaveBeenCalled();
  });

  it('edit mode: rejects saving when the existing order date is now in the past', async () => {
    const user = userEvent.setup();
    const stale = makeOrder({ requested_delivery_date: fromBase(-14) });
    render(<OrderModal open order={stale} onClose={onClose} />);

    await user.click(screen.getByRole('button', { name: '儲存' }));

    expect(screen.getByText(/交貨日必須是明天之後/)).toBeInTheDocument();
    expect(mockUpdateMutate).not.toHaveBeenCalled();
  });

  it('create mode: passes validation if responsible email is empty', async () => {
    const user = userEvent.setup();
    render(<OrderModal open order={undefined} onClose={onClose} />);

    await user.clear(screen.getByLabelText(/客戶名稱/));
    await user.type(screen.getByLabelText(/客戶名稱/), 'Samsung');

    await user.clear(screen.getByLabelText(/晶圓數量/));
    await user.type(screen.getByLabelText(/晶圓數量/), '200');

    await user.type(screen.getByLabelText(/要求交貨日/), FUTURE_DATE);

    // responsible email is left empty

    await user.click(screen.getByRole('button', { name: '新增' }));

    expect(mockCreateMutate).toHaveBeenCalled();
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

  it('edit mode: submit is not blocked when the existing assignee is no longer in the assignable list', async () => {
    // Simulates an assignee who has since been deactivated/removed: the
    // assigned_to user id no longer maps to any entry in useAssignableUsers().
    // The field is disabled in edit mode, so refine must NOT run — otherwise
    // the modal would be permanently unsubmittable.
    const user = userEvent.setup();
    const order = makeOrder({
      id: 'edit-orphan',
      version_id: 7,
      assigned_to: 'uid-DELETED',
    });
    render(<OrderModal open order={order} onClose={onClose} />);

    await user.click(screen.getByRole('button', { name: '儲存' }));

    expect(screen.queryByText('負責人必須是系統中現有的使用者')).not.toBeInTheDocument();
    expect(mockUpdateMutate).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'edit-orphan' }),
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

  // --- error states ---

  describe('error states', () => {
    it('edit mode: shows version-conflict warning on 409', () => {
      updateState = {
        mutate: mockUpdateMutate,
        isPending: false,
        isError: true,
        error: new ApiError(409, 'Order was modified by another user.'),
      };
      render(<OrderModal open order={makeOrder()} onClose={onClose} />);

      expect(screen.getByText('資料版本已更新')).toBeInTheDocument();
      expect(screen.getByText(/此訂單已被其他人修改/)).toBeInTheDocument();
      expect(screen.queryByText(/操作失敗/)).not.toBeInTheDocument();
    });

    it('edit mode: shows generic error text for non-409 errors', () => {
      updateState = {
        mutate: mockUpdateMutate,
        isPending: false,
        isError: true,
        error: new Error('伺服器錯誤，請稍後再試'),
      };
      render(<OrderModal open order={makeOrder()} onClose={onClose} />);

      expect(screen.getByText('伺服器錯誤，請稍後再試')).toBeInTheDocument();
      expect(screen.queryByText('資料版本已更新')).not.toBeInTheDocument();
    });

    it('create mode: shows generic error text on failure', () => {
      createState = {
        mutate: mockCreateMutate,
        isPending: false,
        isError: true,
        error: new Error('建立訂單失敗'),
      };
      render(<OrderModal open order={undefined} onClose={onClose} />);

      expect(screen.getByText('建立訂單失敗')).toBeInTheDocument();
    });
  });
});
