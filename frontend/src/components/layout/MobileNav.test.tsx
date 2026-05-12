/**
 * MobileNav — drawer behaviour at narrow viewports.
 *
 * The drawer must:
 *   * not render until the store opens it (no DOM overhead idle);
 *   * render the same nav items the desktop sidebar shows (driven by
 *     `SidebarNavContent`);
 *   * close on overlay tap / Escape key / route change (so a tap on
 *     "Orders" both navigates AND dismisses the drawer);
 *   * gate the Users link by role (root only) — same rule as desktop.
 *
 * Auth is stubbed via `@/lib/auth`; routing uses MemoryRouter so we can
 * drive `useLocation()` changes without a real history stack.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MobileNav } from './MobileNav';
import { useMobileNavStore } from './mobileNavStore';

const mockRole = { value: 'order_manager' as string | null };

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => ({ id: 'u', username: 'alice', role: mockRole.value }),
  useCurrentRole: () => mockRole.value,
  useCurrentUserId: () => 'u',
}));

function renderWithRouter(initial = '/'): void {
  render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <MobileNav />
              <main data-testid="page" />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

function openDrawer(): void {
  act(() => {
    useMobileNavStore.setState({ open: true });
  });
}

describe('MobileNav', () => {
  beforeEach(() => {
    mockRole.value = 'order_manager';
    useMobileNavStore.setState({ open: false });
  });

  afterEach(() => {
    cleanup();
  });

  it('does not render the drawer when the store is closed', () => {
    renderWithRouter();
    expect(screen.queryByTestId('mobile-nav')).not.toBeInTheDocument();
  });

  it('renders the drawer with nav links when opened', () => {
    renderWithRouter();
    openDrawer();

    expect(screen.getByTestId('mobile-nav')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: /navigation/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /dashboard/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /orders/i })).toBeInTheDocument();
  });

  it('closes when the overlay is tapped', () => {
    renderWithRouter();
    openDrawer();

    fireEvent.click(screen.getByRole('button', { name: /close navigation/i }));
    expect(useMobileNavStore.getState().open).toBe(false);
  });

  it('closes when Escape is pressed', () => {
    renderWithRouter();
    openDrawer();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(useMobileNavStore.getState().open).toBe(false);
  });

  it('closes when a nav link is tapped (route change resets state)', () => {
    renderWithRouter();
    openDrawer();

    // NavLink ``onClick`` fires before the route change effect; both
    // converge to the same close-state.
    fireEvent.click(screen.getByRole('link', { name: /orders/i }));
    expect(useMobileNavStore.getState().open).toBe(false);
  });

  it('hides the Users link for non-root roles', () => {
    mockRole.value = 'order_manager';
    renderWithRouter();
    openDrawer();

    expect(screen.queryByRole('link', { name: /users/i })).not.toBeInTheDocument();
  });

  it('shows the Users link for root', () => {
    mockRole.value = 'root';
    renderWithRouter();
    openDrawer();

    expect(screen.getByRole('link', { name: /users/i })).toBeInTheDocument();
  });
});
