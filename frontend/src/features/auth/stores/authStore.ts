/**
 * Auth Zustand store for derived client-side authentication state.
 *
 * Server state stays in React Query. This store keeps only the bearer token and
 * the minimal identity decoded from the JWT so route guards and layout controls
 * can make local decisions.
 */
import { create } from 'zustand';

import { logout as authApiLogout } from '../api/auth';

const STORAGE_KEY = 'smart-order.auth';

interface AuthUser {
  username: string;
  role: string;
}

interface PersistedAuthState {
  token: string;
  user: AuthUser;
}

interface AuthState {
  token: string | null;
  user: AuthUser | null;
  setToken: (token: string, username?: string) => void;
  logout: () => Promise<void>;
}

function decodeUser(token: string, username?: string): AuthUser {
  try {
    const payload = JSON.parse(atob(token.split('.')[1])) as { role?: string };
    return { username: username ?? 'User', role: payload.role ?? 'viewer' };
  } catch {
    return { username: username ?? 'User', role: 'viewer' };
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
    const parsed = JSON.parse(raw) as Partial<PersistedAuthState>;
    if (!parsed.token || !parsed.user) {
      return null;
    }
    return { token: parsed.token, user: parsed.user };
  } catch {
    window.localStorage.removeItem(STORAGE_KEY);
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
  token: persisted?.token ?? null,
  user: persisted?.user ?? null,

  setToken: (token, username) => {
    const user = decodeUser(token, username);
    persistAuth({ token, user });
    set({ token, user });
  },

  logout: async () => {
    try {
      await authApiLogout();
    } catch {
      // Local logout should still proceed if the server session is already gone.
    }
    persistAuth(null);
    set({ token: null, user: null });
  },
}));
