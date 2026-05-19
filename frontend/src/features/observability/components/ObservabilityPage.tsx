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
import { useSloCompliance } from '../api/useSloCompliance';
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
  ['system', 'slo'],
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
  const slo = useSloCompliance();

  const isFetching =
    systemHealth.isFetching || red.isFetching || resources.isFetching || slo.isFetching;

  // ``dataUpdatedAt`` is always a number in React Query v5 — 0 when the
  // query has never resolved, ms-epoch once it has.
  const lastUpdatedMs = Math.max(red.dataUpdatedAt, resources.dataUpdatedAt, slo.dataUpdatedAt);
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
            <RedKpiCards
              red={red.data}
              redLoading={red.isLoading}
              redError={red.isError}
              slo={slo.data}
              sloLoading={slo.isLoading}
              sloError={slo.isError}
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
