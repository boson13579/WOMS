/**
 * Theme store — light / dark / system, persisted to localStorage.
 *
 * Per RULES.md §2: client-only state goes through Zustand. Theme is the
 * canonical example — it never lives on the server.
 *
 * `system` resolves to whichever the OS reports via the `prefers-color-scheme`
 * media query. The actual application of `dark` to `<html>` happens in
 * `<ThemeProvider>` so this store stays infrastructure-free.
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type Theme = 'light' | 'dark' | 'system';

interface ThemeStore {
  theme: Theme;
  setTheme: (next: Theme) => void;
  /** Cycle light → dark → system → light. Used by the header toggle. */
  cycleTheme: () => void;
}

const NEXT_IN_CYCLE: Record<Theme, Theme> = {
  light: 'dark',
  dark: 'system',
  system: 'light',
};

export const useThemeStore = create<ThemeStore>()(
  persist(
    (set, get) => ({
      theme: 'system',
      setTheme: (next) => {
        set({ theme: next });
      },
      cycleTheme: () => {
        set({ theme: NEXT_IN_CYCLE[get().theme] });
      },
    }),
    { name: 'woms-theme' },
  ),
);

/** Resolve `system` against the OS preference. SSR-safe (returns 'light'). */
export function resolveTheme(theme: Theme): 'light' | 'dark' {
  if (theme !== 'system') return theme;
  if (typeof window === 'undefined') return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
