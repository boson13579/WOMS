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

const authMocks = vi.hoisted(() => ({
  canWrite: true,
}));

vi.mock('@/lib/auth', () => ({
  useCanWrite: () => authMocks.canWrite,
  useCurrentUserId: () => null,
}));

vi.mock('@/features/auth/api/users', () => ({
  useUsers: () => [],
  useAssignableUsers: () => [
    { id: 'u-1', username: 'alice', email: 'alice@example.com' },
    { id: 'u-2', username: 'bob', email: 'bob@example.com' },
  ],
}));

// ---------------------------------------------------------------------------
// Mock useOrderStore
// ---------------------------------------------------------------------------

const mockSetStatus = vi.fn();
const mockSetSearch = vi.fn();
const mockSetAssignedTo = vi.fn();
const mockSetCreatedBy = vi.fn();
const mockReset = vi.fn();

const mockStore = {
  status: null as string | null,
  search: '',
  assignedTo: [] as string[],
  createdBy: [] as string[],
  page: 1,
  setStatus: mockSetStatus,
  setSearch: mockSetSearch,
  setAssignedTo: mockSetAssignedTo,
  setCreatedBy: mockSetCreatedBy,
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
    mockStore.assignedTo = [];
    mockStore.createdBy = [];
    authMocks.canWrite = true;
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

  it('calls setAssignedTo with the matching user id when a username is typed', async () => {
    const user = userEvent.setup();
    render(<OrderFilters />);

    await user.type(screen.getByRole('combobox', { name: /搜尋負責人/ }), 'alice');

    await waitFor(
      () => {
        expect(mockSetAssignedTo).toHaveBeenCalledWith(['u-1']);
      },
      { timeout: 1000 },
    );
  });

  it('calls setCreatedBy with the matching user id when a username is typed', async () => {
    const user = userEvent.setup();
    render(<OrderFilters />);

    await user.type(screen.getByRole('combobox', { name: /搜尋建立者/ }), 'bob');

    await waitFor(
      () => {
        expect(mockSetCreatedBy).toHaveBeenCalledWith(['u-2']);
      },
      { timeout: 1000 },
    );
  });

  it('does not call setAssignedTo when the typed name matches no user', async () => {
    const user = userEvent.setup();
    render(<OrderFilters />);

    await user.type(screen.getByRole('combobox', { name: /搜尋負責人/ }), 'nobody');

    // Wait past the 300ms debounce so we can assert nothing fires.
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(mockSetAssignedTo).not.toHaveBeenCalled();
  });

  it('clears the assignee filter when the input is emptied (store had a value)', async () => {
    mockStore.assignedTo = ['u-1'];
    const user = userEvent.setup();
    render(<OrderFilters />);

    // Initial value hydrated from the store.
    const input = screen.getByRole('combobox', { name: /搜尋負責人/ });
    await waitFor(() => {
      expect(input).toHaveValue('alice');
    });

    await user.clear(input);

    await waitFor(
      () => {
        expect(mockSetAssignedTo).toHaveBeenCalledWith([]);
      },
      { timeout: 1000 },
    );
  });

  it('reset button clears local assignee/creator inputs', async () => {
    const user = userEvent.setup();
    render(<OrderFilters />);

    const assigneeInput = screen.getByRole('combobox', { name: /搜尋負責人/ });
    const creatorInput = screen.getByRole('combobox', { name: /搜尋建立者/ });

    await user.type(assigneeInput, 'alice');
    await user.type(creatorInput, 'bob');
    expect(assigneeInput).toHaveValue('alice');
    expect(creatorInput).toHaveValue('bob');

    await user.click(screen.getByRole('button', { name: /重設/ }));

    expect(assigneeInput).toHaveValue('');
    expect(creatorInput).toHaveValue('');
    expect(mockReset).toHaveBeenCalledOnce();
  });

  it('hides assignee/creator filter inputs for viewer role (cannot write)', () => {
    // viewer 沒有 /users/assignable 權限，看到 input 卻搜不到人是壞 UX。
    authMocks.canWrite = false;
    render(<OrderFilters />);

    // Order keyword search and status select still render.
    expect(screen.getByRole('textbox', { name: /搜尋訂單/ })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: /篩選狀態/ })).toBeInTheDocument();

    // But the user-pickers are hidden.
    expect(screen.queryByRole('combobox', { name: /搜尋負責人/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: /搜尋建立者/ })).not.toBeInTheDocument();
  });
});
