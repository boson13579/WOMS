/**
 * Tests for ``AuthOnlyRoute`` — the inverse guard sibling of
 * ``ProtectedRoute``. Authed users hitting /login or /register should
 * be redirected back to ``?next=…`` (default ``/``) so a bookmarked
 * link doesn't strand them on the login form.
 */
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { useAuthStore } from '@/features/auth/stores/authStore';

import { AuthOnlyRoute } from './AuthOnlyRoute';

const ANY_USER = { id: 'u', username: 'alice', role: 'viewer' };

function setSession(future: boolean): void {
  useAuthStore.setState({
    user: ANY_USER,
    expiresAt: future ? Date.now() + 10_000 : Date.now() - 10_000,
  });
}

beforeEach(() => {
  useAuthStore.setState({ user: null, expiresAt: null });
});

afterEach(() => {
  useAuthStore.setState({ user: null, expiresAt: null });
});

describe('AuthOnlyRoute', () => {
  // RED: AuthOnlyRoute doesn't exist yet.
  it('renders children when no session is persisted', () => {
    render(
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route
            path="/login"
            element={
              <AuthOnlyRoute>
                <div>login form</div>
              </AuthOnlyRoute>
            }
          />
          <Route path="/" element={<div>home</div>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText('login form')).toBeInTheDocument();
  });

  it('redirects authed users with a future expiresAt to /', () => {
    setSession(true);

    render(
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route
            path="/login"
            element={
              <AuthOnlyRoute>
                <div>login form</div>
              </AuthOnlyRoute>
            }
          />
          <Route path="/" element={<div>home</div>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText('home')).toBeInTheDocument();
    expect(screen.queryByText('login form')).not.toBeInTheDocument();
  });

  it('honours ?next= when redirecting', () => {
    setSession(true);

    render(
      <MemoryRouter initialEntries={['/login?next=/orders']}>
        <Routes>
          <Route
            path="/login"
            element={
              <AuthOnlyRoute>
                <div>login form</div>
              </AuthOnlyRoute>
            }
          />
          <Route path="/orders" element={<div>orders page</div>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText('orders page')).toBeInTheDocument();
  });

  it('renders children when the persisted expiresAt is in the past', () => {
    setSession(false);

    render(
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route
            path="/login"
            element={
              <AuthOnlyRoute>
                <div>login form</div>
              </AuthOnlyRoute>
            }
          />
          <Route path="/" element={<div>home</div>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText('login form')).toBeInTheDocument();
  });
});
