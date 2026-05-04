/**
 * Main dashboard composition — assembles the four tiers of widgets.
 *
 * Tier 1 (top):    4 service-health cards (API / DB / Redis / Celery)
 * Tier 2:          4 resource-utilization cards with sparklines
 * Tier 3 (charts): Request rate + Latency, side-by-side
 * Tier 4 (bottom): Orders snapshot + Recent activity
 *
 * Server-state via `useDashboardData()` (React Query). The widget components
 * themselves are presentational — they take their data via props — so they
 * can be tested in isolation without a QueryClient.
 */
import { useQueryClient } from '@tanstack/react-query';
import { AlertCircle } from 'lucide-react';

import { Header } from '@/components/layout/Header';
import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

import { DASHBOARD_QUERY_KEY, useDashboardData } from '../api/useDashboardData';

import { ActivityFeed } from './ActivityFeed';
import { LatencyChart } from './LatencyChart';
import { MetricCard } from './MetricCard';
import { OrdersSnapshotCard } from './OrdersSnapshotCard';
import { RequestRateChart } from './RequestRateChart';
import { StatusCard } from './StatusCard';

export function DashboardPage(): JSX.Element {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error, isFetching, dataUpdatedAt, refetch } =
    useDashboardData();

  const onRefresh = (): void => {
    // `invalidate` triggers a refetch and shows the spinner via `isFetching`.
    void queryClient.invalidateQueries({ queryKey: DASHBOARD_QUERY_KEY });
    void refetch();
  };

  const lastLabel = dataUpdatedAt ? formatRelative(new Date(dataUpdatedAt)) : '—';

  const renderBody = (): JSX.Element | null => {
    if (isLoading) return <DashboardSkeleton />;
    if (isError) {
      return <DashboardError message={error instanceof Error ? error.message : 'Unknown error'} />;
    }
    if (!data) return null;
    return (
      <>
        {/* Tier 1 — service health */}
        <section aria-label="Service health">
          <SectionLabel>Services</SectionLabel>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {data.services.map((s) => (
              <StatusCard key={s.id} service={s} />
            ))}
          </div>
        </section>

        {/* Tier 2 — resource utilization */}
        <section aria-label="Resource utilization">
          <SectionLabel>Resources</SectionLabel>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {data.resources.map((m) => (
              <MetricCard key={m.id} metric={m} />
            ))}
          </div>
        </section>

        {/* Tier 3 — application metrics */}
        <section aria-label="Application metrics">
          <SectionLabel>Application</SectionLabel>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <RequestRateChart series={data.requestRate} />
            <LatencyChart series={data.latency} />
          </div>
        </section>

        {/* Tier 4 — orders + activity */}
        <section aria-label="Business metrics" className="pb-6">
          <SectionLabel>Orders &amp; activity</SectionLabel>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <div className="lg:col-span-1">
              <OrdersSnapshotCard snapshot={data.orders} />
            </div>
            <div className="lg:col-span-2">
              <ActivityFeed items={data.activity} />
            </div>
          </div>
        </section>
      </>
    );
  };

  return (
    <>
      <Header
        title="Dashboard"
        subtitle="System health and utilization · Phase 1 mock"
        lastUpdatedLabel={lastLabel}
        onRefresh={onRefresh}
        refreshing={isFetching}
      />

      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto flex max-w-[1400px] flex-col gap-6">{renderBody()}</div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function SectionLabel({ children }: { children: React.ReactNode }): JSX.Element {
  return (
    <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
      {children}
    </h2>
  );
}

function DashboardSkeleton(): JSX.Element {
  return (
    <div data-testid="dashboard-skeleton" className="flex flex-col gap-6">
      {/* Match the four-tier layout so the page doesn't reflow on data arrival. */}
      <SkeletonRow count={4} cardClass="h-32" />
      <SkeletonRow count={4} cardClass="h-32" />
      <SkeletonRow count={2} cardClass="h-72" />
      <SkeletonRow count={2} cardClass="h-64" />
    </div>
  );
}

/**
 * Skeleton row using only Tailwind classes (no inline style — RULES.md §2).
 * Only `count = 2 | 4` are used by the dashboard skeleton; we encode each
 * column layout explicitly so Tailwind's purger keeps the classes.
 */
function SkeletonRow({ count, cardClass }: { count: 2 | 4; cardClass: string }): JSX.Element {
  return (
    <div
      className={cn(
        'grid gap-4',
        count === 4 && 'grid-cols-1 sm:grid-cols-2 xl:grid-cols-4',
        count === 2 && 'grid-cols-1 sm:grid-cols-2',
      )}
    >
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} className={cardClass} />
      ))}
    </div>
  );
}

function DashboardError({ message }: { message: string }): JSX.Element {
  return (
    <Card className="border-destructive/40">
      <CardContent className="flex items-start gap-3 p-6">
        <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
        <div>
          <p className="font-medium">Failed to load dashboard.</p>
          <p className="mt-1 text-sm text-muted-foreground">{message}</p>
        </div>
      </CardContent>
    </Card>
  );
}

function formatRelative(d: Date): string {
  const seconds = Math.max(1, Math.round((Date.now() - d.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins} min ago`;
  return `${Math.round(mins / 60)} h ago`;
}
