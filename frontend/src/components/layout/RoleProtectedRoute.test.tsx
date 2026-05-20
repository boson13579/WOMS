/**
 * Tests for ``RoleProtectedRoute`` — nested role gate sibling of
 * ``ProtectedRoute``.
 *
 *   - role in ``allowedRoles`` → renders ``<Outlet />``
 *   - role NOT in ``allowedRoles`` → redirects to ``/`` (dashboard)
 *   - role is ``null`` (defensive — ``ProtectedRoute`` should have
 *     short-circuited first) → redirects to ``/``
 *   - multi-role ``allowedRoles`` → passes for any matching role
 *
 * The gate composes inside ``ProtectedRoute`` so we don't repeat the
 * server-confirmed identity flow here; we mock ``useCurrentRole`` and
 * verify the redirect / pass-through behaviour in isolation.
 */
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { UserRole } from '@/lib/auth';

import { RoleProtectedRoute } from './RoleProtectedRoute';

const { mockCurrentRole } = vi.hoisted(() => ({
  mockCurrentRole: vi.fn<() => UserRole | null>(),
}));

vi.mock('@/lib/auth', () => ({
  useCurrentRole: mockCurrentRole,
}));

afterEach(() => {
  mockCurrentRole.mockReset();
});

function renderWithRouter(allowedRoles: UserRole[]): void {
  render(
    <MemoryRouter initialEntries={['/users']}>
      <Routes>
        <Route path="/" element={<div>dashboard home</div>} />
        <Route element={<RoleProtectedRoute allowedRoles={allowedRoles} />}>
          <Route path="/users" element={<div>users admin page</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe('RoleProtectedRoute', () => {
  // RED: component does not yet exist.
  it('renders the nested Outlet when the current role is in allowedRoles', () => {
    mockCurrentRole.mockReturnValue('root');

    renderWithRouter(['root']);

    expect(screen.getByText('users admin page')).toBeInTheDocument();
    expect(screen.queryByText('dashboard home')).not.toBeInTheDocument();
  });

  it('redirects to / when the current role is not in allowedRoles', () => {
    mockCurrentRole.mockReturnValue('viewer');

    renderWithRouter(['root']);

    expect(screen.getByText('dashboard home')).toBeInTheDocument();
    expect(screen.queryByText('users admin page')).not.toBeInTheDocument();
  });

  it('redirects to / when the current role is null (defensive)', () => {
    // ProtectedRoute should have caught this case before this gate even
    // mounts, but a defensive redirect avoids a hook ordering trap if a
    // future refactor reorders the layout tree.
    mockCurrentRole.mockReturnValue(null);

    renderWithRouter(['root']);

    expect(screen.getByText('dashboard home')).toBeInTheDocument();
    expect(screen.queryByText('users admin page')).not.toBeInTheDocument();
  });

  it('passes for any role listed when allowedRoles has multiple entries', () => {
    mockCurrentRole.mockReturnValue('scheduler');

    renderWithRouter(['root', 'scheduler']);

    expect(screen.getByText('users admin page')).toBeInTheDocument();
  });

  it('still redirects when role is none of multiple allowedRoles', () => {
    mockCurrentRole.mockReturnValue('order_manager');

    renderWithRouter(['root', 'scheduler']);

    expect(screen.getByText('dashboard home')).toBeInTheDocument();
    expect(screen.queryByText('users admin page')).not.toBeInTheDocument();
  });
});
