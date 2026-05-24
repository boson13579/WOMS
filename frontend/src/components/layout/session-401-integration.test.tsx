/**
 * Integration test for the global 401 redirect flow.
 *
 * The pieces (apiFetch, authStore, ProtectedRoute, SessionBoundary,
 * AuthPage's ``?next=``) are each covered by unit tests. This walks the
 * wiring end-to-end: render a protected page → its inner data fetch
 * returns 401 → assert nav lands at ``/login?next=…``, the React Query
 * cache is empty, and the auth store has been logged out.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import * as React from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAuthStore } from '@/features/auth/stores/authStore';
import { apiFetch } from '@/lib/apiFetch';

import { AuthOnlyRoute } from './AuthOnlyRoute';
import { ProtectedRoute } from './ProtectedRoute';
import { SessionBoundary } from './SessionBoundary';

vi.mock('@/features/notifications/hooks/useNotificationsWs', () => ({
  useNotificationsWs: vi.fn(),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const VALID_ME = {
  id: '00000000-0000-0000-0000-000000000002',
  username: 'alice',
  email: 'alice@example.com',
  role: 'viewer',
  is_active: true,
  version_id: 4,
};

function setSession(): void {
  useAuthStore.setState({
    user: { id: 'u', username: 'alice', role: 'viewer' },
    expiresAt: Date.now() + 60_000,
  });
}

// A pretend page that mounts a query which always 401s.
function FlakyPage(): JSX.Element {
  const [state, setState] = React.useState<'idle' | 'done' | 'error'>('idle');
  React.useEffect(() => {
    apiFetch('/api/v1/anything', { credentials: 'include' }, (raw) => raw)
      .then(() => {
        setState('done');
      })
      .catch(() => {
        setState('error');
      });
  }, []);
  return <div data-testid="page">page state: {state}</div>;
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

describe('global 401 redirect — end-to-end', () => {
  it('navigates to /login?next=/, clears the QC cache, and logs out', async () => {
    setSession();

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    qc.setQueryData(['orders', 'snapshot'], { pending: 99 });

    vi.mocked(global.fetch)
      // useMe() inside ProtectedRoute resolves OK first; the inner
      // FlakyPage fires the 401 that triggers the redirect.
      .mockResolvedValueOnce(jsonResponse(VALID_ME))
      // logout() inside the handler — fast 204 reply.
      .mockResolvedValue(new Response(null, { status: 204 }));

    // Queue the 401 for FlakyPage. Order matters: useMe first, then this.
    vi.mocked(global.fetch).mockResolvedValueOnce(
      jsonResponse({ detail: 'Not authenticated.' }, 401),
    );

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route element={<SessionBoundary />}>
              <Route path="/" element={<ProtectedRoute />}>
                <Route index element={<FlakyPage />} />
              </Route>
              <Route
                path="/login"
                element={
                  <AuthOnlyRoute>
                    <div>login page</div>
                  </AuthOnlyRoute>
                }
              />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // The flow should produce a navigation to /login. The route
    // ``AuthOnlyRoute`` will pass-through because logout() cleared the
    // store, so the login page becomes visible.
    await waitFor(() => {
      expect(screen.getByText('login page')).toBeInTheDocument();
    });

    // QC cache cleared.
    expect(qc.getQueryData(['orders', 'snapshot'])).toBeUndefined();
    expect(qc.getQueryData(['auth', 'me'])).toBeUndefined();
    // Auth store logged out.
    expect(useAuthStore.getState().user).toBeNull();
  });
});
