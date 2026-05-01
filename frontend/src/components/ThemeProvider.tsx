/**
 * ThemeProvider — applies the current theme to `<html>` and watches the
 * OS-level `prefers-color-scheme` change so `system` mode follows the user
 * flipping their device theme without a reload.
 *
 * Pure side-effect; renders children unchanged.
 */
import { useEffect } from 'react';

import { resolveTheme, useThemeStore } from '@/stores/themeStore';

interface ThemeProviderProps {
  children: React.ReactNode;
}

export function ThemeProvider({ children }: ThemeProviderProps): React.ReactNode {
  const theme = useThemeStore((s) => s.theme);

  useEffect(() => {
    const apply = (): void => {
      const resolved = resolveTheme(theme);
      document.documentElement.classList.toggle('dark', resolved === 'dark');
      document.documentElement.style.colorScheme = resolved;
    };

    apply();

    // Only listen to OS changes when in `system` mode — otherwise the explicit
    // user choice should win and OS flips are irrelevant.
    if (theme !== 'system') return undefined;

    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    mq.addEventListener('change', apply);
    return () => {
      mq.removeEventListener('change', apply);
    };
  }, [theme]);

  return children;
}
