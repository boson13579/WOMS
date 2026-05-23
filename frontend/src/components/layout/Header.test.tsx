/**
 * Tests for ``Header`` — verifies the logout flow clears the React
 * Query cache before calling ``logout()`` so stale per-user data
 * doesn't leak across sessions on the same machine.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAuthStore } from '@/features/auth/stores/authStore';

import { Header } from './Header';

let qc: QueryClient;
let logoutSpy: ReturnType<typeof vi.fn>;

function renderHeader() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(['orders', 'snapshot'], { pending: 7 });

  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Header title="Test" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  logoutSpy = vi.fn(async () => {});
  useAuthStore.setState({
    user: { id: 'u', username: 'alice', role: 'viewer' },
    expiresAt: Date.now() + 60_000,
    logout: logoutSpy,
  });
  // Stub fetch so the default logout HTTP path doesn't fire.
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useAuthStore.setState({ user: null, expiresAt: null });
});

describe('Header — logout flow', () => {
  // RED: handleLogout currently does not call queryClient.clear().
  it('clears the React Query cache when Logout is clicked', async () => {
    renderHeader();
    expect(qc.getQueryData(['orders', 'snapshot'])).toEqual({ pending: 7 });

    fireEvent.click(screen.getByRole('button', { name: /Logout/i }));

    await waitFor(() => {
      expect(qc.getQueryData(['orders', 'snapshot'])).toBeUndefined();
    });
    expect(logoutSpy).toHaveBeenCalled();
  });
});
