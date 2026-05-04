/**
 * Auth Zustand store — client-side auth state only.
 *
 * Per RULES.md §2: server state lives in React Query; only the derived
 * client-side "is the user authenticated?" and the decoded identity live here.
 *
 * Phase 2: replace `setToken` to also write to `localStorage`/`sessionStorage`
 * with `httpOnly` cookie support depending on the final security decision.
 */
import { create } from 'zustand';

interface AuthUser {
  username: string;
}

interface AuthState {
  /** Raw JWT token; null when logged out. */
  token: string | null;
  /** Minimal decoded identity. Null when logged out. */
  user: AuthUser | null;
  /** Persist a successful login response into the store. */
  setToken: (token: string, username: string) => void;
  /** Clear auth state on logout. */
  logout: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  user: null,

  setToken: (token, username) => {
    set({ token, user: { username } });
  },

  logout: () => {
    set({ token: null, user: null });
  },
}));
