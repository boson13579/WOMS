import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAuthStore } from '@/features/auth/stores/authStore';

import { deactivateUser, listUsers, updateUser } from '../api/users';
import type { UserResponse } from '../types/user';

import { AdminUsersPage } from './AdminUsersPage';

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
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }

  return { wrapper: Wrapper };
}

function renderPage(role = 'root') {
  act(() => {
    useAuthStore.setState({
      user: { id: `${role}_id`, username: `${role}_user`, role },
      expiresAt: Date.now() + 60_000,
    });
  });

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
  act(() => {
    useAuthStore.setState({ user: null, expiresAt: null });
  });
});

describe('AdminUsersPage permissions', () => {
  it.each(['viewer', 'scheduler', 'order_manager'])(
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
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['users'] });
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
