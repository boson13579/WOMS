/**
 * Tests for ``ProtectedRoute`` — server-confirmed identity gate.
 *
 *   - No persisted session → synchronous redirect to ``/login``
 *   - ``useMe()`` pending → splash (no <Outlet> render)
 *   - ``useMe()`` 200 → <Outlet> renders
 *   - ``useMe()`` 401 → handled by the global handler (apiFetch fires it
 *     and ``ApiError(401)`` is thrown; the Outlet must NOT render)
 *   - ``useMe()`` 5xx → error-state card (Retry + Sign out), splash gone
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAuthStore } from '@/features/auth/stores/authStore';

import { ProtectedRoute } from './ProtectedRoute';

const VALID_ME = {
  id: '00000000-0000-0000-0000-000000000002',
  username: 'alice',
  email: 'alice@example.com',
  role: 'viewer',
  is_active: true,
  version_id: 4,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function setSession(): void {
  useAuthStore.setState({
    user: { id: 'u', username: 'alice', role: 'viewer' },
    expiresAt: Date.now() + 60_000,
  });
}

function renderWithRouter(initialPath = '/'): QueryClient {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/" element={<ProtectedRoute />}>
            <Route index element={<div>Protected page</div>} />
          </Route>
          <Route path="/login" element={<div>Login page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return qc;
}

beforeEach(() => {
  useAuthStore.setState({ user: null, expiresAt: null });
  vi.spyOn(globalThis, 'fetch');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useAuthStore.setState({ user: null, expiresAt: null });
});

describe('ProtectedRoute', () => {
  // RED: current implementation redirects on clock check only; useMe not wired.
  it('redirects to /login synchronously when no session is persisted', () => {
    renderWithRouter('/');
    expect(screen.getByText('Login page')).toBeInTheDocument();
    expect(screen.queryByText('Protected page')).not.toBeInTheDocument();
  });

  it('renders a splash while useMe() is pending', async () => {
    setSession();
    // Return a never-resolving fetch so the query stays pending.
    vi.mocked(global.fetch).mockImplementationOnce(() => new Promise(() => {}));

    renderWithRouter('/');
    expect(await screen.findByRole('status')).toBeInTheDocument();
    expect(screen.queryByText('Protected page')).not.toBeInTheDocument();
  });

  it('renders the Outlet on a successful useMe() response', async () => {
    setSession();
    vi.mocked(global.fetch).mockResolvedValueOnce(jsonResponse(VALID_ME));

    renderWithRouter('/');
    await waitFor(() => {
      expect(screen.getByText('Protected page')).toBeInTheDocument();
    });
  });

  it('does not render the Outlet when useMe() returns 401', async () => {
    setSession();
    vi.mocked(global.fetch).mockResolvedValueOnce(
      jsonResponse({ detail: 'Not authenticated.' }, 401),
    );

    renderWithRouter('/');
    // Wait for the query to settle.
    await waitFor(() => {
      expect(screen.queryByText('Protected page')).not.toBeInTheDocument();
    });
    // The global handler is responsible for navigation — without it
    // wired, the boundary just renders nothing useful. Either way, no
    // Outlet.
  });

  it('renders a Retry / Sign out error card on non-401 useMe() failures', async () => {
    setSession();
    vi.mocked(global.fetch).mockResolvedValueOnce(jsonResponse({ detail: 'Boom' }, 500));

    renderWithRouter('/');

    await waitFor(() => {
      expect(screen.getByText(/Couldn't verify session/i)).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Sign out/i })).toBeInTheDocument();
  });

  it('Retry refetches useMe and renders the Outlet on success', async () => {
    setSession();
    vi.mocked(global.fetch)
      .mockResolvedValueOnce(jsonResponse({ detail: 'Boom' }, 500))
      .mockResolvedValueOnce(jsonResponse(VALID_ME));

    renderWithRouter('/');

    await waitFor(() => {
      expect(screen.getByText(/Couldn't verify session/i)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /Retry/i }));

    await waitFor(() => {
      expect(screen.getByText('Protected page')).toBeInTheDocument();
    });
  });
});
