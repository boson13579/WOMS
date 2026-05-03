/**
 * React Query hook for the dashboard overview.
 *
 * Per RULES.md §2: server-state must go through React Query, never raw
 * imports of "data constants". Phase 1 backend is mocked but the hook is
 * already correctly shaped — Phase 2 just swaps `fetchDashboardOverview`'s
 * implementation under the hood and the rest of the app sees no diff.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import type { DashboardOverview } from '../types';

import { fetchDashboardOverview } from './dashboard';

export const DASHBOARD_QUERY_KEY = ['dashboard', 'overview'] as const;

/**
 * Subscribe to the dashboard overview snapshot.
 *
 * Refetch cadence: 30 s while the tab is focused. Long enough not to hammer
 * the API; short enough that an operator glancing back gets fresh-ish data
 * without manual refresh.
 */
export function useDashboardData(): UseQueryResult<DashboardOverview> {
  return useQuery({
    queryKey: DASHBOARD_QUERY_KEY,
    queryFn: fetchDashboardOverview,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}
