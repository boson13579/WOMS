/**
 * Tests for ``AuthPage`` — focuses on the ``?next=`` propagation contract
 * introduced for the global-401 redirect flow.
 *
 * When a user is bounced to ``/login?next=/orders`` by the unauthorized
 * handler, a successful login should send them back to ``/orders``,
 * not the default ``/``. This is verified by stubbing ``useNavigate`` to
 * a spy and triggering ``handleLoginSuccess`` indirectly through the
 * default (no ``onLoginSuccess`` prop) code path.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type * as ReactRouterDom from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAuthStore } from '@/features/auth/stores/authStore';

import { AuthPage } from './AuthPage';

const navigateSpy = vi.fn();

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof ReactRouterDom>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigateSpy,
  };
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/login" element={<AuthPage />} />
          <Route path="/register" element={<AuthPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  navigateSpy.mockReset();
  useAuthStore.setState({ user: null, expiresAt: null });
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    jsonResponse({
      access_token: 'eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1IiwiZXhwIjoxMDAwMDAwMDAwMH0.sig',
      token_type: 'bearer',
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useAuthStore.setState({ user: null, expiresAt: null });
});

describe('AuthPage — login navigation', () => {
  // RED: AuthPage currently always navigates to '/' on success.
  it('navigates to ?next= on successful login', async () => {
    renderAt('/login?next=/orders');

    fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: 'Password1' } });
    fireEvent.click(screen.getByRole('button', { name: /Sign in/i }));

    await waitFor(() => {
      expect(navigateSpy).toHaveBeenCalledWith('/orders', { replace: true });
    });
  });

  it('defaults to / when no ?next= is present', async () => {
    renderAt('/login');

    fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: 'Password1' } });
    fireEvent.click(screen.getByRole('button', { name: /Sign in/i }));

    await waitFor(() => {
      expect(navigateSpy).toHaveBeenCalledWith('/', { replace: true });
    });
  });
});
