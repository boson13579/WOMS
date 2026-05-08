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
import { logout as authApiLogout } from '../api/auth';

interface AuthUser {
  username: string;
  role: string;
}

interface AuthState {
  /** Raw JWT token; null when logged out. */
  token: string | null;
  /** Minimal decoded identity. Null when logged out. */
  user: AuthUser | null;
  /** Persist a successful login response into the store. */
  setToken: (token: string, username?: string) => void;
  /** Clear auth state on logout. */
  logout: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  user: null,

  setToken: (token, username) => {
    try {
      const payload = JSON.parse(atob(token.split('.')[1]));
      // The backend puts user_id in sub, role in role. We don't get username from token in standard OAuth,
      // but if username is provided during login, we can use it. Otherwise, we just store the role.
      set({ token, user: { username: username ?? 'User', role: payload.role } });
    } catch {
      set({ token, user: { username: username ?? 'User', role: 'viewer' } });
    }
  },

  logout: async () => {
    try {
      await authApiLogout();
    } catch {
      // Ignore failures on logout
    }
    set({ token: null, user: null });
  },
}));
