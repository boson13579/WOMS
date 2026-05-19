/**
 * Auth Zustand store for derived client-side authentication state.
 *
 * Server state stays in React Query. The access token is kept in an httpOnly
 * cookie by the backend; this store persists only non-sensitive identity and
 * expiry metadata for route guards and layout controls.
 *
 * The persisted ``expiresAt`` is a hint, not the source of truth — the
 * cookie may have been invalidated server-side (logout from another tab,
 * role change, deactivation). ``useMe()`` in ``lib/auth.ts`` provides
 * server-confirmed identity; any 401 funnels through the
 * ``unauthorizedHandler`` installed via ``installUnauthorizedHandler``.
 */
import type { QueryClient } from '@tanstack/react-query';
import { create } from 'zustand';

import { setUnauthorizedHandler } from '@/lib/apiFetch';

import { logout as authApiLogout } from '../api/auth';

const STORAGE_KEY = 'smart-order.auth';

/**
 * Fallback TTL applied when a JWT lacks a usable ``exp`` claim (or the
 * payload segment is corrupt). Mirrors backend
 * ``JWT_ACCESS_TOKEN_TTL_SECONDS = 3600`` (see
 * ``backend/app/core/config.py:66``). If those two ever drift, the
 * persisted hint will be wrong by the difference — but ``useMe()`` will
 * surface the real state on the next focus.
 */
export const JWT_DEFAULT_TTL_MS = 60 * 60 * 1000;

interface AuthUser {
  id: string;
  username: string;
  role: string;
}

interface PersistedAuthState {
  user: AuthUser;
  expiresAt: number;
}

interface AuthState {
  user: AuthUser | null;
  expiresAt: number | null;
  setSession: (token: string, username?: string) => void;
  logout: () => Promise<void>;
}

function decodeJwtPayload(token: string): { sub?: string; role?: string; exp?: number } {
  const payloadSegment = token.split('.')[1];
  if (!payloadSegment) {
    return {};
  }

  const normalizedPayload = payloadSegment.replace(/-/g, '+').replace(/_/g, '/');
  const paddedPayload = normalizedPayload.padEnd(
    normalizedPayload.length + ((4 - (normalizedPayload.length % 4)) % 4),
    '=',
  );
  return JSON.parse(atob(paddedPayload)) as { sub?: string; role?: string; exp?: number };
}

function decodeSession(token: string, username?: string): PersistedAuthState {
  try {
    const payload = decodeJwtPayload(token);
    return {
      user: { id: payload.sub ?? '', username: username ?? 'User', role: payload.role ?? 'viewer' },
      expiresAt:
        typeof payload.exp === 'number' ? payload.exp * 1000 : Date.now() + JWT_DEFAULT_TTL_MS,
    };
  } catch {
    return {
      user: { id: '', username: username ?? 'User', role: 'viewer' },
      expiresAt: Date.now() + JWT_DEFAULT_TTL_MS,
    };
  }
}

function isExpired(expiresAt: number): boolean {
  return expiresAt <= Date.now();
}

function clearPersistedAuth(): void {
  if (typeof window !== 'undefined') {
    window.localStorage.removeItem(STORAGE_KEY);
  }
}

function loadPersistedAuth(): PersistedAuthState | null {
  if (typeof window === 'undefined') {
    return null;
  }

  const raw = window.localStorage.getItem(STORAGE_KEY);
  if (!raw) {
    return null;
  }

  try {
    const parsed = JSON.parse(raw) as Partial<PersistedAuthState> & { token?: string };
    if (parsed.token) {
      const migrated = decodeSession(parsed.token, parsed.user?.username);
      if (isExpired(migrated.expiresAt)) {
        clearPersistedAuth();
        return null;
      }
      persistAuth(migrated);
      return migrated;
    }
    if (!parsed.user || !parsed.expiresAt || isExpired(parsed.expiresAt)) {
      clearPersistedAuth();
      return null;
    }
    return { user: parsed.user, expiresAt: parsed.expiresAt };
  } catch {
    clearPersistedAuth();
    return null;
  }
}

function persistAuth(state: PersistedAuthState | null): void {
  if (typeof window === 'undefined') {
    return;
  }

  if (state === null) {
    window.localStorage.removeItem(STORAGE_KEY);
    return;
  }

  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

const persisted = loadPersistedAuth();

export const useAuthStore = create<AuthState>((set) => ({
  user: persisted?.user ?? null,
  expiresAt: persisted?.expiresAt ?? null,

  setSession: (token, username) => {
    const session = decodeSession(token, username);
    persistAuth(session);
    set({ user: session.user, expiresAt: session.expiresAt });
  },

  logout: async () => {
    try {
      await authApiLogout();
    } catch {
      // Local logout should still proceed if the server session is already gone.
    }
    persistAuth(null);
    set({ user: null, expiresAt: null });
  },
}));

/**
 * Wire a process-wide handler that funnels every 401 — wherever it
 * surfaces — through the same cleanup path: clear the React Query cache
 * (prevents stale data leaking across sessions), local logout (drops the
 * persisted user hint), and ``navigate('/login?next=<current>')`` so the
 * post-login redirect lands the user where they were.
 *
 * Idempotent by design: calling twice replaces the handler. Install once
 * at app boot inside the router tree (so ``navigate`` is bound to the
 * router context).
 */
export function installUnauthorizedHandler(
  queryClient: QueryClient,
  navigate: (path: string, opts?: { replace?: boolean }) => void,
): void {
  setUnauthorizedHandler(() => {
    queryClient.clear();
    // Run logout async — the user's local state is already torn down by
    // the time the handler returns; the HTTP POST to /auth/logout is
    // best-effort and doesn't block the navigation.
    void useAuthStore.getState().logout();
    const next =
      typeof window === 'undefined' ? '/' : `${window.location.pathname}${window.location.search}`;
    navigate(`/login?next=${encodeURIComponent(next)}`, { replace: true });
  });
}
