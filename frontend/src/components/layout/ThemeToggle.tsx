/**
 * Theme toggle button — cycles light → dark → system → light.
 *
 * Icon shown reflects the *current* theme (sun = light, moon = dark,
 * monitor = system); tooltip says what the next click will do.
 */
import { Monitor, Moon, Sun } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { useThemeStore, type Theme } from '@/stores/themeStore';

const ICON: Record<Theme, LucideIcon> = {
  light: Sun,
  dark: Moon,
  system: Monitor,
};

const NEXT_LABEL: Record<Theme, string> = {
  light: 'Switch to dark mode',
  dark: 'Switch to system theme',
  system: 'Switch to light mode',
};

export function ThemeToggle(): JSX.Element {
  const theme = useThemeStore((s) => s.theme);
  const cycleTheme = useThemeStore((s) => s.cycleTheme);
  const Icon = ICON[theme];

  return (
    <Button
      type="button"
      variant="outline"
      size="icon"
      aria-label={NEXT_LABEL[theme]}
      title={NEXT_LABEL[theme]}
      onClick={cycleTheme}
    >
      <Icon className="h-4 w-4" />
    </Button>
  );
}
