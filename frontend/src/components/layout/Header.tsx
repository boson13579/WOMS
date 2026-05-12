/**
 * Top app bar.
 *
 * Page title lives on the left; lightweight contextual actions live on the
 * right. The sidebar owns primary navigation.
 */
import { LogOut, RefreshCcw, Search } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import { ThemeToggle } from '@/components/layout/ThemeToggle';
import { Button } from '@/components/ui/button';
import { useAuthStore } from '@/features/auth/stores/authStore';

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
}: HeaderProps): JSX.Element {
  const navigate = useNavigate();
  const logout = useAuthStore((state) => state.logout);

  const handleLogout = () => {
    void logout().finally(() => {
      navigate('/login', { replace: true });
    });
  };

  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-border bg-background px-6">
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-lg font-semibold tracking-tight">{title}</h1>
        {subtitle ? <p className="truncate text-xs text-muted-foreground">{subtitle}</p> : null}
      </div>

      <div className="relative hidden items-center md:flex">
        <Search className="pointer-events-none absolute left-2.5 h-4 w-4 text-muted-foreground" />
        <input
          type="search"
          disabled
          placeholder="Search (Phase 2)"
          className="h-9 w-56 cursor-not-allowed rounded-md border border-border bg-muted/40 pl-8 pr-3 text-sm text-muted-foreground placeholder:text-muted-foreground/70 focus-visible:outline-none"
        />
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
