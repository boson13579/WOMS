/**
 * OrderFilters — search input and status select.
 *
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { OrderFilters } from './OrderFilters';

// ---------------------------------------------------------------------------
// Mock @/lib/auth and users
// ---------------------------------------------------------------------------

vi.mock('@/lib/auth', () => ({
  useCanWrite: () => false,
  useCurrentUserId: () => null,
}));

vi.mock('@/features/auth/api/users', () => ({
  useUsers: () => [],
}));

// ---------------------------------------------------------------------------
// Mock useOrderStore
// ---------------------------------------------------------------------------

const mockSetStatus = vi.fn();
const mockSetSearch = vi.fn();
const mockReset = vi.fn();

const mockStore = {
  status: null as string | null,
  search: '',
  assignedTo: [] as string[],
  createdBy: [] as string[],
  page: 1,
  setStatus: mockSetStatus,
  setSearch: mockSetSearch,
  setAssignedTo: vi.fn(),
  setCreatedBy: vi.fn(),
  setPage: vi.fn(),
  reset: mockReset,
};

vi.mock('../stores/orderStore', () => ({
  useOrderStore: () => mockStore,
}));

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('OrderFilters', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockStore.status = null;
    mockStore.search = '';
  });

  it('renders the search input and status select', () => {
    render(<OrderFilters />);

    expect(screen.getByRole('textbox', { name: /搜尋訂單/ })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: /篩選狀態/ })).toBeInTheDocument();
  });

  it('renders the reset button', () => {
    render(<OrderFilters />);

    expect(screen.getByRole('button', { name: /重設/ })).toBeInTheDocument();
  });

  it('calls reset() when the reset button is clicked', async () => {
    const user = userEvent.setup();
    render(<OrderFilters />);

    await user.click(screen.getByRole('button', { name: /重設/ }));

    expect(mockReset).toHaveBeenCalledOnce();
  });

  it('calls setStatus() when the status select changes', async () => {
    const user = userEvent.setup();
    render(<OrderFilters />);

    await user.selectOptions(screen.getByRole('combobox', { name: /篩選狀態/ }), 'pending');

    expect(mockSetStatus).toHaveBeenCalledWith('pending');
  });

  it('calls setStatus(null) when "all statuses" is selected', async () => {
    const user = userEvent.setup();
    mockStore.status = 'pending';
    render(<OrderFilters />);

    await user.selectOptions(screen.getByRole('combobox', { name: /篩選狀態/ }), '');

    expect(mockSetStatus).toHaveBeenCalledWith(null);
  });

  it('debounces the search input — setSearch fires once not per keystroke', async () => {
    const user = userEvent.setup();
    render(<OrderFilters />);

    await user.type(screen.getByRole('textbox', { name: /搜尋訂單/ }), 'TSM');

    await waitFor(
      () => {
        expect(mockSetSearch).toHaveBeenCalledWith('TSM');
      },
      { timeout: 1000 },
    );
    // debounce: called once with the final value, not once per character typed
    expect(mockSetSearch).toHaveBeenCalledOnce();
  });
});
