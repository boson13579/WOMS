/**
 * Dashboard composition — role-gated, real-API edition.
 *
 * Layout (per `notes/dashboard-implementation-plan.md`):
 *   * Tier 1: ScheduleControlBar (scheduler+) + ScheduleStatusCard
 *   * Tier 2: CapacityChart (full width)
 *   * Tier 3: PendingOpsTable + OrdersSnapshotCard
 *   * Tier 4: ServiceHealthGrid
 *
 * Viewer-role users get the simplified {@link ViewerDashboard}; new
 * registrations default to viewer until root promotes them.
 *
 * All server state is React Query — components stay pure presentational.
 * WebSocket-driven invalidation lands in a follow-up PR; for now we poll
 * (see hook files for cadence).
 */
import { useQueryClient } from '@tanstack/react-query';

import { Header } from '@/components/layout/Header';
import { useCurrentRole } from '@/lib/auth';

import { useOrdersSnapshot } from '../api/useOrdersSnapshot';
import { usePendingOps } from '../api/usePendingOps';
import { useScheduleCapacity } from '../api/useScheduleCapacity';
import { useScheduleStatus } from '../api/useScheduleStatus';
import { useSystemHealth } from '../api/useSystemHealth';

import { CapacityChart } from './CapacityChart';
import { OrdersSnapshotCard } from './OrdersSnapshotCard';
import { PendingOpsTable } from './PendingOpsTable';
import { ScheduleControlBar } from './ScheduleControlBar';
import { ScheduleStatusCard } from './ScheduleStatusCard';
import { ServiceHealthGrid } from './ServiceHealthGrid';
import { ViewerDashboard } from './ViewerDashboard';

const DASHBOARD_INVALIDATE_PREFIXES = [
  ['system', 'health'],
  ['schedule', 'status'],
  ['schedule', 'capacity'],
  ['schedule', 'pending-ops'],
  ['orders', 'snapshot'],
];

export function DashboardPage(): JSX.Element {
  const role = useCurrentRole();
  const queryClient = useQueryClient();

  const systemHealth = useSystemHealth();
  const scheduleStatus = useScheduleStatus();
  const capacity = useScheduleCapacity();
  const pendingOps = usePendingOps();
  const ordersSnapshot = useOrdersSnapshot();

  const isFetching =
    systemHealth.isFetching ||
    scheduleStatus.isFetching ||
    capacity.isFetching ||
    pendingOps.isFetching ||
    ordersSnapshot.isFetching;

  // ``onRefresh`` triggers a refetch on every dashboard query at once.
  // React Query's ``invalidate`` flips the `isFetching` flag synchronously
  // so the Header's spinner state updates without a frame delay.
  const onRefresh = (): void => {
    DASHBOARD_INVALIDATE_PREFIXES.forEach((queryKey) => {
      void queryClient.invalidateQueries({ queryKey });
    });
  };

  // Viewer (the default for new registrations) gets a simplified page.
  // We render the full Header below so layout is consistent across roles.
  const showViewerView = role === 'viewer' || role === null;

  return (
    <>
      {showViewerView ? (
        <Header title="Dashboard" subtitle="Read-only view" />
      ) : (
        <Header
          title="Dashboard"
          subtitle="Real-time scheduler operations"
          onRefresh={onRefresh}
          refreshing={isFetching}
        />
      )}

      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto flex max-w-[1400px] flex-col gap-6">
          {showViewerView ? (
            <ViewerDashboard />
          ) : (
            <>
              {/* Tier 1 — schedule controls + status badge */}
              <section
                aria-label="Schedule control"
                className="flex flex-col gap-4 lg:flex-row lg:items-stretch"
              >
                <div className="lg:flex-1">
                  <ScheduleStatusCard
                    data={scheduleStatus.data}
                    isLoading={scheduleStatus.isLoading}
                    isError={scheduleStatus.isError}
                    queueDepth={pendingOps.data?.length ?? 0}
                  />
                </div>
                <div className="lg:flex-1">
                  <ScheduleControlBar />
                </div>
              </section>

              {/* Tier 2 — capacity prefix sum */}
              <section aria-label="Capacity">
                <CapacityChart
                  data={capacity.data}
                  isLoading={capacity.isLoading}
                  isError={capacity.isError}
                />
              </section>

              {/* Tier 3 — pending ops + orders snapshot */}
              <section aria-label="Queue and orders">
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                  <div className="lg:col-span-2">
                    <PendingOpsTable
                      data={pendingOps.data}
                      isLoading={pendingOps.isLoading}
                      isError={pendingOps.isError}
                    />
                  </div>
                  <OrdersSnapshotCard
                    data={ordersSnapshot.data}
                    isLoading={ordersSnapshot.isLoading}
                    isError={ordersSnapshot.isError}
                  />
                </div>
              </section>

              {/* Tier 4 — service health */}
              <section aria-label="Service health">
                <SectionLabel>Services</SectionLabel>
                <ServiceHealthGrid
                  data={systemHealth.data}
                  isLoading={systemHealth.isLoading}
                  isError={systemHealth.isError}
                />
              </section>
            </>
          )}
        </div>
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
