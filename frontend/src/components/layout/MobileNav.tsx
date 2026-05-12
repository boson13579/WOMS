/**
 * Mobile navigation drawer.
 *
 * The persistent `Sidebar` is hidden below `md` (768px), so without a
 * fallback users on narrow viewports get stuck on whatever route they
 * loaded — no way to reach Orders / Dashboard / Users from the UI. This
 * drawer is the mobile-only replacement: a slide-in panel triggered by
 * the hamburger button in `Header`, dismissed by tapping outside, by
 * pressing Escape, or by navigating to a new route.
 *
 * Reuses `SidebarNavContent` so the desktop and mobile nav stay in
 * lockstep — adding a new section only needs editing `Sidebar.tsx`.
 *
 * Custom implementation rather than `Sheet` from shadcn so we don't pull
 * in `@radix-ui/react-dialog` for a single drawer.
 */
import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';

import { useMobileNavStore } from './mobileNavStore';
import { SidebarNavContent } from './Sidebar';

export function MobileNav(): JSX.Element | null {
  const open = useMobileNavStore((s) => s.open);
  const setOpen = useMobileNavStore((s) => s.setOpen);
  const location = useLocation();

  // Close on route change. Without this the drawer would still be open
  // after the user taps a nav link, covering the destination page.
  useEffect(() => {
    setOpen(false);
  }, [location.pathname, setOpen]);

  // ESC closes — standard dialog convention.
  useEffect(() => {
    if (!open) return undefined;
    function onKey(e: KeyboardEvent): void {
      if (e.key === 'Escape') setOpen(false);
    }
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
    };
  }, [open, setOpen]);

  if (!open) return null;

  return (
    <div className="md:hidden" data-testid="mobile-nav">
      <button
        type="button"
        aria-label="Close navigation"
        onClick={() => {
          setOpen(false);
        }}
        className="fixed inset-0 z-40 bg-background/80 backdrop-blur-sm"
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Navigation"
        className="fixed inset-y-0 left-0 z-50 w-60 border-r border-border bg-card shadow-lg"
      >
        <SidebarNavContent
          onNavigate={() => {
            setOpen(false);
          }}
        />
      </aside>
    </div>
  );
}
