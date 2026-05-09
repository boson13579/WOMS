/**
 * Auth Zustand store for derived client-side authentication state.
 *
 * Server state stays in React Query. The access token is kept in an httpOnly
 * cookie by the backend; this store persists only non-sensitive identity and
 * expiry metadata for route guards and layout controls.
 */
import { create } from 'zustand';

import { logout as authApiLogout } from '../api/auth';

const STORAGE_KEY = 'smart-order.auth';

interface AuthUser {
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

function decodeJwtPayload(token: string): { role?: string; exp?: number } {
  const payloadSegment = token.split('.')[1];
  if (!payloadSegment) {
    return {};
  }

  const normalizedPayload = payloadSegment.replace(/-/g, '+').replace(/_/g, '/');
  const paddedPayload = normalizedPayload.padEnd(
    normalizedPayload.length + ((4 - (normalizedPayload.length % 4)) % 4),
    '=',
  );
  return JSON.parse(atob(paddedPayload)) as { role?: string; exp?: number };
}

function decodeSession(token: string, username?: string): PersistedAuthState {
  try {
    const payload = decodeJwtPayload(token);
    return {
      user: { username: username ?? 'User', role: payload.role ?? 'viewer' },
      expiresAt: typeof payload.exp === 'number' ? payload.exp * 1000 : 0,
    };
  } catch {
    return { user: { username: username ?? 'User', role: 'viewer' }, expiresAt: 0 };
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
