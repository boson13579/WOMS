/**
 * Top app bar.
 *
 * Page title lives on the left; lightweight contextual actions live on the
 * right. The sidebar owns primary navigation.
 */
import { useQueryClient } from '@tanstack/react-query';
import { LogOut, Menu, RefreshCcw } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import { useMobileNavStore } from '@/components/layout/mobileNavStore';
import { ThemeToggle } from '@/components/layout/ThemeToggle';
import { Button } from '@/components/ui/button';
import { useAuthStore } from '@/features/auth/stores/authStore';
import { useNotifications } from '@/features/notifications/api/notifications';

interface HeaderProps {
  title: string;
  subtitle?: string;
  /** Last-refreshed label, displayed on the right. */
  lastUpdatedLabel?: string;
  /** Optional refresh handler. Disabled state shows a spinning icon. */
  onRefresh?: () => void;
  refreshing?: boolean;
}

export function Header({
  title,
  subtitle,
  lastUpdatedLabel,
  onRefresh,
  refreshing = false,
}: Readonly<HeaderProps>): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const logout = useAuthStore((state) => state.logout);
  const openMobileNav = useMobileNavStore((state) => state.setOpen);

  // Backend contract: when called with `all=false`, the `/api/v1/notifications`
  // endpoint returns only unread items and `total` is the unread count. The
  // sidebar relies on the same field; if that contract ever changes, both
  // callers need to switch to counting `items` (and re-evaluate pagination).
  const { data: unreadData } = useNotifications({ all: false });
  const unreadCount = unreadData?.total ?? 0;

  const handleLogout = () => {
    // Clear server-state cache BEFORE the logout HTTP call so a slow
    // /auth/logout (or a network error during it) never leaves stale
    // per-user data visible while the redirect is in flight. The auth
    // store also clears its own local hint inside logout().
    queryClient.clear();
    logout()
      .finally(() => {
        navigate('/login', { replace: true });
      })
      .catch(() => {});
  };

  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-border bg-background px-4 md:px-6">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={() => {
          openMobileNav(true);
        }}
        aria-label="Open navigation"
        className="relative md:hidden"
      >
        <Menu className="h-5 w-5" />
        {unreadCount > 0 && (
          <span
            aria-label={`${unreadCount} unread notifications`}
            aria-live="polite"
            className="absolute right-1.5 top-1.5 flex h-2 w-2 rounded-full bg-rose-500 ring-2 ring-background animate-pulse"
          />
        )}
      </Button>
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-lg font-semibold tracking-tight">{title}</h1>
        {subtitle ? <p className="truncate text-xs text-muted-foreground">{subtitle}</p> : null}
      </div>

      {lastUpdatedLabel ? (
        <span className="hidden text-xs text-muted-foreground md:inline">
          Updated {lastUpdatedLabel}
        </span>
      ) : null}

      {onRefresh ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onRefresh}
          disabled={refreshing}
          aria-label="Refresh metrics"
        >
          <RefreshCcw className={`mr-2 h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      ) : null}

      <ThemeToggle />
      <Button type="button" variant="outline" size="sm" onClick={handleLogout}>
        <LogOut className="mr-2 h-3.5 w-3.5" aria-hidden="true" />
        Logout
      </Button>
    </header>
  );
}
