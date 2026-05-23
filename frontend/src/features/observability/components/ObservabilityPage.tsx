/**
 * ObservabilityPage — RED + USE operator dashboard.
 *
 * Sections (top to bottom):
 *   1. Service health (reuses ``ServiceHealthGrid`` from the dashboard).
 *   2. RED row: 4 KPI cards + time-range pills.
 *   3. USE row: 3 resource cards including the workers drilldown.
 *   4. Top endpoints table.
 *
 * Role gate is enforced at the route level (``RoleProtectedRoute
 * allowedRoles=['root','scheduler']``); this component renders
 * unconditionally for the allowed roles. The shell mirrors the
 * dashboard's ``px-6 py-6 mx-auto max-w-[1400px]`` so the two pages
 * read as siblings.
 */
import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { Header } from '@/components/layout/Header';
import { useSystemHealth } from '@/features/dashboard/api/useSystemHealth';
import { ServiceHealthGrid } from '@/features/dashboard/components/ServiceHealthGrid';

import { useRedMetrics } from '../api/useRedMetrics';
import { useScheduleLag } from '../api/useScheduleLag';
import { useUseResources } from '../api/useUseResources';
import { RED_WINDOW_OPTIONS, type RedWindowSeconds } from '../types';

import { RedEndpointsTable } from './RedEndpointsTable';
import { RedKpiCards } from './RedKpiCards';
import { TimeRangeSelector } from './TimeRangeSelector';
import { UseResourceCards } from './UseResourceCards';

const DEFAULT_WINDOW: RedWindowSeconds = RED_WINDOW_OPTIONS[0];

const INVALIDATE_PREFIXES = [
  ['system', 'red'],
  ['system', 'resources'],
  ['system', 'schedule-lag'],
  ['system', 'health'],
];

function SectionLabel({ children }: { children: React.ReactNode }): JSX.Element {
  return (
    <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
      {children}
    </h2>
  );
}

function formatLastUpdated(epochMs: number): string | undefined {
  if (epochMs === 0) return undefined;
  const d = new Date(epochMs);
  return d.toLocaleTimeString();
}

function formatWindowLabel(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

export function ObservabilityPage(): JSX.Element {
  const queryClient = useQueryClient();
  const [windowSeconds, setWindowSeconds] = useState<RedWindowSeconds>(DEFAULT_WINDOW);

  const systemHealth = useSystemHealth();
  const red = useRedMetrics(windowSeconds);
  const resources = useUseResources();
  const lag = useScheduleLag(windowSeconds);

  const isFetching =
    systemHealth.isFetching || red.isFetching || resources.isFetching || lag.isFetching;

  // ``dataUpdatedAt`` is always a number in React Query v5 — 0 when the
  // query has never resolved, ms-epoch once it has.
  const lastUpdatedMs = Math.max(red.dataUpdatedAt, resources.dataUpdatedAt, lag.dataUpdatedAt);
  const lastUpdatedLabel = formatLastUpdated(lastUpdatedMs);

  const onRefresh = (): void => {
    INVALIDATE_PREFIXES.forEach((queryKey) => {
      void queryClient.invalidateQueries({ queryKey });
    });
  };

  return (
    <>
      <Header
        title="Observability"
        subtitle={`RED + USE · last ${formatWindowLabel(windowSeconds)}`}
        onRefresh={onRefresh}
        refreshing={isFetching}
        {...(lastUpdatedLabel ? { lastUpdatedLabel } : {})}
      />

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[1400px] space-y-6 px-6 py-6">
          {/* Services */}
          <section aria-label="Service health">
            <SectionLabel>Services</SectionLabel>
            <ServiceHealthGrid
              data={systemHealth.data}
              isLoading={systemHealth.isLoading}
              isError={systemHealth.isError}
            />
          </section>

          {/* RED block */}
          <section aria-label="RED metrics">
            <div className="mb-3 flex items-center gap-3">
              <SectionLabel>RED · Requests</SectionLabel>
              <div className="ml-auto">
                <TimeRangeSelector value={windowSeconds} onChange={setWindowSeconds} />
              </div>
            </div>
            {/*
             * Degraded-data banner. RED reads the request-sample ZSET in
             * Redis — if the source is unreachable, the backend returns
             * the zero envelope with ``data_status === 'degraded'``.
             * Without this banner, the dashboard would silently render
             * an all-green "0 req/s" state during a metrics outage and
             * the operator would mistake the outage for healthy quiet.
             * Schedule lag reads a separate ZSET; if RED degrades, lag
             * has almost certainly also degraded but we don't surface a
             * separate banner for it — the existing one already covers
             * "metrics data is unavailable" generically.
             */}
            {red.data?.data_status === 'degraded' && (
              <div
                role="status"
                data-testid="metrics-degraded-banner"
                className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-700 dark:bg-amber-900/30 dark:text-amber-200"
              >
                Metrics data is currently unavailable (Redis unreachable). Numbers shown may not
                reflect live state.
              </div>
            )}
            <RedKpiCards
              red={red.data}
              redLoading={red.isLoading}
              redError={red.isError}
              lag={lag.data}
              lagLoading={lag.isLoading}
              lagError={lag.isError}
            />
          </section>

          {/* USE block */}
          <section aria-label="USE resources">
            <SectionLabel>USE · Resources</SectionLabel>
            <UseResourceCards
              data={resources.data}
              isLoading={resources.isLoading}
              isError={resources.isError}
            />
          </section>

          {/* Top endpoints */}
          <section aria-label="Top endpoints">
            <SectionLabel>Top endpoints</SectionLabel>
            <RedEndpointsTable
              data={red.data?.by_endpoint}
              isLoading={red.isLoading}
              isError={red.isError}
              windowSeconds={windowSeconds}
            />
          </section>
        </div>
      </div>
    </>
  );
}
