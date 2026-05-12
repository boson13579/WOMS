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
 * in `@radix-ui/react-dialog` for a single drawer. We re-implement the
 * dialog contract by hand: ESC dismisses, body scroll is locked while
 * the drawer is open (otherwise touch scrolling on mobile drags the
 * page behind the drawer), and Tab is cycled inside the panel so
 * keyboard users don't wander into hidden content underneath.
 */
import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';

import { useMobileNavStore } from './mobileNavStore';
import { SidebarNavContent } from './Sidebar';

export function MobileNav(): JSX.Element | null {
  const open = useMobileNavStore((s) => s.open);
  const setOpen = useMobileNavStore((s) => s.setOpen);
  const location = useLocation();
  const drawerRef = useRef<HTMLElement>(null);

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

  // Lock body scroll while open. Without this, swiping inside the
  // drawer scrolls the page behind it on touch devices.
  useEffect(() => {
    if (!open) return undefined;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  // Focus management: move focus to the first link on open, and cycle
  // Tab within the drawer so keyboard users can't escape into the
  // hidden page underneath.
  useEffect(() => {
    if (!open) return undefined;
    const drawer = drawerRef.current;
    if (!drawer) return undefined;
    const focusables = Array.from(
      drawer.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    );
    if (focusables.length === 0) return undefined;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    first.focus();

    function onKey(e: KeyboardEvent): void {
      if (e.key !== 'Tab') return;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
    drawer.addEventListener('keydown', onKey);
    return () => {
      drawer.removeEventListener('keydown', onKey);
    };
  }, [open]);

  if (!open) return null;

  return (
    <div className="md:hidden" data-testid="mobile-nav">
      <button
        type="button"
        aria-label="Close navigation"
        tabIndex={-1}
        onClick={() => {
          setOpen(false);
        }}
        className="fixed inset-0 z-40 bg-background/80 backdrop-blur-sm"
      />
      <aside
        ref={drawerRef}
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
