/**
 * Top app bar.
 *
 * Notion-inspired: page title left, lightweight contextual actions right.
 * No deep nav here — the sidebar owns navigation.
 */
import { RefreshCcw, Search } from 'lucide-react';

import { ThemeToggle } from '@/components/layout/ThemeToggle';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

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
  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-border bg-background px-6">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <h1 className="truncate text-lg font-semibold tracking-tight">{title}</h1>
          <Badge variant="outline" className="border-dashed">
            mock data
          </Badge>
        </div>
        {subtitle ? <p className="truncate text-xs text-muted-foreground">{subtitle}</p> : null}
      </div>

      {/* Search affordance — disabled in Phase 1 */}
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
    </header>
  );
}
