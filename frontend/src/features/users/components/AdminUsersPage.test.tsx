import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { deactivateUser, listUsers, updateUser } from '../api/users';
import type { UserResponse, UserRole } from '../types/user';

import { AdminUsersPage } from './AdminUsersPage';

const { mockCurrentRole, mockCurrentUserId } = vi.hoisted(() => ({
  mockCurrentRole: vi.fn<() => UserRole | null>(),
  mockCurrentUserId: vi.fn<() => string | null>(),
}));

vi.mock('@/lib/auth', () => ({
  useCurrentRole: mockCurrentRole,
  useCurrentUserId: mockCurrentUserId,
}));

vi.mock('../api/users', () => ({
  listUsers: vi.fn(),
  updateUser: vi.fn(),
  deactivateUser: vi.fn(),
}));

const USERS: UserResponse[] = [
  {
    id: '00000000-0000-0000-0000-000000000001',
    username: 'root_admin',
    email: 'root@example.com',
    role: 'root',
    is_active: true,
    version_id: 1,
    created_at: '2026-05-04T00:00:00.000Z',
  },
  {
    id: '00000000-0000-0000-0000-000000000002',
    username: 'alice',
    email: 'alice@example.com',
    role: 'viewer',
    is_active: true,
    version_id: 2,
    created_at: '2026-05-04T00:00:00.000Z',
  },
  {
    id: '00000000-0000-0000-0000-000000000003',
    username: 'inactive_user',
    email: null,
    role: 'order_manager',
    is_active: false,
    version_id: 3,
    created_at: '2026-05-04T00:00:00.000Z',
  },
];

function makeWrapper(): { wrapper: ({ children }: { children: ReactNode }) => JSX.Element } {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });

  function Wrapper({ children }: { children: ReactNode }): JSX.Element {
    return (
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </MemoryRouter>
    );
  }

  return { wrapper: Wrapper };
}

function renderPage(role: UserRole = 'root', currentUserId: string | null = null) {
  mockCurrentRole.mockReturnValue(role);
  mockCurrentUserId.mockReturnValue(currentUserId);

  const { wrapper: Wrapper } = makeWrapper();
  return render(<AdminUsersPage />, { wrapper: Wrapper });
}

function mockUserList(users = USERS): void {
  vi.mocked(listUsers).mockResolvedValue({ users, total: users.length });
}

beforeEach(() => {
  vi.mocked(updateUser).mockImplementation((_userId, payload) =>
    Promise.resolve({
      ...USERS[1],
      role: payload.role ?? USERS[1].role,
      is_active: payload.is_active ?? USERS[1].is_active,
      version_id: USERS[1].version_id + 1,
    }),
  );
  vi.mocked(deactivateUser).mockImplementation((userId) =>
    Promise.resolve({
      ...(USERS.find((user) => user.id === userId) ?? USERS[1]),
      is_active: false,
    }),
  );
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('AdminUsersPage permissions', () => {
  it.each<UserRole>(['viewer', 'scheduler', 'order_manager'])(
    'shows a root-only message for %s users and does not fetch accounts',
    (role) => {
      renderPage(role);

      expect(screen.getByText(/root access required/i)).toBeInTheDocument();
      expect(screen.getByText(/available only to root users/i)).toBeInTheDocument();
      expect(listUsers).not.toHaveBeenCalled();
    },
  );

  it('renders the user table for root users', async () => {
    mockUserList();
    renderPage('root');

    expect(await screen.findByRole('heading', { name: /user management/i })).toBeInTheDocument();
    expect(await screen.findByText('root_admin')).toBeInTheDocument();
    expect(screen.getByText('alice')).toBeInTheDocument();
    expect(screen.getByText('inactive_user')).toBeInTheDocument();
    expect(screen.getByText('No email')).toBeInTheDocument();
    expect(listUsers).toHaveBeenCalledWith('');
  });

  it('shows an empty state when no users match the query', async () => {
    mockUserList([]);
    renderPage('root');

    expect(await screen.findByText(/no users found/i)).toBeInTheDocument();
  });

  it('shows backend errors as alerts', async () => {
    vi.mocked(listUsers).mockRejectedValueOnce(new Error('Only root users can manage accounts.'));
    renderPage('root');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Only root users can manage accounts.');
  });
});

describe('AdminUsersPage root operations', () => {
  it('searches users by the typed query', async () => {
    mockUserList();
    renderPage('root');

    await screen.findByText('alice');
    await userEvent.type(screen.getByLabelText(/search users/i), 'alice');

    await waitFor(() => {
      expect(listUsers).toHaveBeenLastCalledWith('alice');
    });
  });

  it('edits a user role and active status', async () => {
    mockUserList();
    renderPage('root');

    const aliceRow = (await screen.findByText('alice')).closest('tr');
    expect(aliceRow).not.toBeNull();

    await userEvent.click(within(aliceRow as HTMLElement).getByRole('button', { name: /edit/i }));
    await userEvent.selectOptions(screen.getByLabelText(/role for alice/i), 'scheduler');
    await userEvent.click(screen.getByLabelText(/^active$/i));
    await userEvent.click(within(aliceRow as HTMLElement).getByRole('button', { name: /save/i }));

    await waitFor(() => {
      expect(updateUser).toHaveBeenCalledWith(USERS[1].id, {
        role: 'scheduler',
        is_active: false,
        version_id: 2,
      });
    });
  });

  it('can set every supported role while editing', async () => {
    mockUserList();
    renderPage('root');

    const aliceRow = (await screen.findByText('alice')).closest('tr');
    expect(aliceRow).not.toBeNull();
    await userEvent.click(within(aliceRow as HTMLElement).getByRole('button', { name: /edit/i }));

    const roleSelect = screen.getByLabelText(/role for alice/i);
    expect(within(roleSelect).getByRole('option', { name: 'Root' })).toBeInTheDocument();
    expect(within(roleSelect).getByRole('option', { name: 'Scheduler' })).toBeInTheDocument();
    expect(within(roleSelect).getByRole('option', { name: 'Order Manager' })).toBeInTheDocument();
    expect(within(roleSelect).getByRole('option', { name: 'Viewer' })).toBeInTheDocument();

    await userEvent.selectOptions(roleSelect, 'root');
    await userEvent.click(within(aliceRow as HTMLElement).getByRole('button', { name: /save/i }));

    await waitFor(() => {
      expect(updateUser).toHaveBeenCalledWith(
        USERS[1].id,
        expect.objectContaining({ role: 'root' }),
      );
    });
  });

  it('cancels editing without sending an update', async () => {
    mockUserList();
    renderPage('root');

    const aliceRow = (await screen.findByText('alice')).closest('tr');
    expect(aliceRow).not.toBeNull();

    await userEvent.click(within(aliceRow as HTMLElement).getByRole('button', { name: /edit/i }));
    await userEvent.selectOptions(screen.getByLabelText(/role for alice/i), 'scheduler');
    await userEvent.click(within(aliceRow as HTMLElement).getByRole('button', { name: /cancel/i }));

    expect(updateUser).not.toHaveBeenCalled();
    expect(
      within(aliceRow as HTMLElement).getByRole('button', { name: /edit/i }),
    ).toBeInTheDocument();
  });

  it('deactivates an active user', async () => {
    mockUserList();
    renderPage('root');

    const aliceRow = (await screen.findByText('alice')).closest('tr');
    expect(aliceRow).not.toBeNull();

    await userEvent.click(
      within(aliceRow as HTMLElement).getByRole('button', { name: /deactivate/i }),
    );

    await waitFor(() => {
      expect(deactivateUser).toHaveBeenCalledWith(USERS[1].id, expect.any(Object));
    });
  });

  it('disables deactivate for inactive users', async () => {
    mockUserList();
    renderPage('root');

    const inactiveRow = (await screen.findByText('inactive_user')).closest('tr');
    expect(inactiveRow).not.toBeNull();

    expect(
      within(inactiveRow as HTMLElement).getByRole('button', { name: /deactivate/i }),
    ).toBeDisabled();
  });

  it('invalidates the users query after successful update and deactivate', async () => {
    mockUserList();
    const invalidateSpy = vi.spyOn(QueryClient.prototype, 'invalidateQueries');
    renderPage('root');

    const aliceRow = (await screen.findByText('alice')).closest('tr');
    expect(aliceRow).not.toBeNull();

    await userEvent.click(within(aliceRow as HTMLElement).getByRole('button', { name: /edit/i }));
    await userEvent.click(within(aliceRow as HTMLElement).getByRole('button', { name: /save/i }));
    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['admin-users'] });
    });

    await userEvent.click(
      within(aliceRow as HTMLElement).getByRole('button', { name: /deactivate/i }),
    );
    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledTimes(2);
    });

    invalidateSpy.mockRestore();
  });
});

describe('AdminUsersPage self-action guard', () => {
  // RED: own-row controls do not yet disable when ``useCurrentUserId``
  // matches the row's user id. Backend ``_guard_last_root`` is a safety
  // net for the last-root case only; the UI guard is broader (any self
  // edit / deactivate by root is disallowed for UX defence-in-depth).
  const OWN_ROW_TOOLTIP = 'Cannot modify your own account';

  it("disables the role select on the current root user's own row while editing", async () => {
    mockUserList();
    // ``USERS[0]`` is root_admin, the current user in this scenario.
    renderPage('root', USERS[0].id);

    const ownRow = (await screen.findByText('root_admin')).closest('tr');
    expect(ownRow).not.toBeNull();

    await userEvent.click(within(ownRow as HTMLElement).getByRole('button', { name: /edit/i }));

    const roleSelect = within(ownRow as HTMLElement).getByLabelText(/role for root_admin/i);
    expect(roleSelect).toBeDisabled();
    // Tooltip / aria-disabled is on the surrounding cell wrappers so
    // screen readers can announce why the controls are inert; the row
    // has three guarded cells (role, status, actions), each carrying
    // the same title.
    const tooltipCells = within(ownRow as HTMLElement).getAllByTitle(OWN_ROW_TOOLTIP);
    expect(tooltipCells.length).toBeGreaterThan(0);
  });

  it('disables the active checkbox on the own row while editing', async () => {
    mockUserList();
    renderPage('root', USERS[0].id);

    const ownRow = (await screen.findByText('root_admin')).closest('tr');
    expect(ownRow).not.toBeNull();
    await userEvent.click(within(ownRow as HTMLElement).getByRole('button', { name: /edit/i }));

    const activeCheckbox = within(ownRow as HTMLElement).getByLabelText(/^active$/i);
    expect(activeCheckbox).toBeDisabled();
  });

  it('disables the Deactivate button on the own row even when the row is active', async () => {
    mockUserList();
    renderPage('root', USERS[0].id);

    const ownRow = (await screen.findByText('root_admin')).closest('tr');
    expect(ownRow).not.toBeNull();

    const deactivateBtn = within(ownRow as HTMLElement).getByRole('button', {
      name: /deactivate/i,
    });
    expect(deactivateBtn).toBeDisabled();
    expect(within(ownRow as HTMLElement).getAllByTitle(OWN_ROW_TOOLTIP).length).toBeGreaterThan(0);
  });

  it('leaves other rows fully editable when the current user is root_admin (regression)', async () => {
    mockUserList();
    renderPage('root', USERS[0].id);

    const aliceRow = (await screen.findByText('alice')).closest('tr');
    expect(aliceRow).not.toBeNull();

    // Deactivate button stays enabled on other active rows.
    expect(
      within(aliceRow as HTMLElement).getByRole('button', { name: /deactivate/i }),
    ).toBeEnabled();

    // Open edit and confirm role + active controls are enabled.
    await userEvent.click(within(aliceRow as HTMLElement).getByRole('button', { name: /edit/i }));
    expect(within(aliceRow as HTMLElement).getByLabelText(/role for alice/i)).toBeEnabled();
    expect(within(aliceRow as HTMLElement).getByLabelText(/^active$/i)).toBeEnabled();
    // And no own-row tooltip leaked onto alice's row.
    expect(within(aliceRow as HTMLElement).queryAllByTitle(OWN_ROW_TOOLTIP)).toHaveLength(0);
  });

  it('does not lock any row when useCurrentUserId returns null (defensive)', async () => {
    mockUserList();
    // Defensive path: store user is missing but role is still root (could
    // only happen via partial state corruption). No row should match
    // ``null`` so every row stays editable.
    renderPage('root', null);

    const rootRow = (await screen.findByText('root_admin')).closest('tr');
    expect(rootRow).not.toBeNull();

    expect(
      within(rootRow as HTMLElement).getByRole('button', { name: /deactivate/i }),
    ).toBeEnabled();
    await userEvent.click(within(rootRow as HTMLElement).getByRole('button', { name: /edit/i }));
    expect(within(rootRow as HTMLElement).getByLabelText(/role for root_admin/i)).toBeEnabled();
    expect(within(rootRow as HTMLElement).getByLabelText(/^active$/i)).toBeEnabled();
    expect(within(rootRow as HTMLElement).queryAllByTitle(OWN_ROW_TOOLTIP)).toHaveLength(0);
  });

  it('keeps the tooltip text visible on at least one own-row cell (getByTitle)', async () => {
    // Verifier-suggested polish: assert the explanatory copy literally
    // renders so a future refactor can't silently drop the title= prop.
    // Also asserts aria-disabled is set so assistive tech can announce
    // why the cell is inert.
    mockUserList();
    renderPage('root', USERS[0].id);

    const ownRow = (await screen.findByText('root_admin')).closest('tr');
    expect(ownRow).not.toBeNull();

    const tooltipCells = within(ownRow as HTMLElement).getAllByTitle(OWN_ROW_TOOLTIP);
    expect(tooltipCells.length).toBeGreaterThan(0);
    expect(tooltipCells[0]).toHaveAttribute('aria-disabled', 'true');
  });
});
