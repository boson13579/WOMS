/**
 * useSloCompliance — fetches the SLO + error-budget snapshot.
 *
 * Backed by ``GET /api/v1/system/slo?window_hours=…``. Polled at 30s
 * because the underlying value moves slowly — a 24h SLO window only
 * shifts noticeably when sustained errors land, so anything faster
 * spends backend cycles aggregating the same answer. The page's
 * RED-row SLO card colour-bands on ``success_pct`` vs ``slo_target_pct``
 * and ``error_budget_pct_remaining``.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiFetch } from '@/lib/apiFetch';
import { useCurrentUser } from '@/lib/auth';

import { sloComplianceSchema, type SloCompliance } from '../types';

const REFETCH_INTERVAL_MS = 30_000;
const STALE_TIME_MS = 15_000;
const DEFAULT_WINDOW_HOURS = 24;

export function sloComplianceQueryKey(windowHours: number): readonly unknown[] {
  return ['system', 'slo', windowHours] as const;
}

export function useSloCompliance(
  windowHours: number = DEFAULT_WINDOW_HOURS,
): UseQueryResult<SloCompliance> {
  const user = useCurrentUser();

  return useQuery<SloCompliance>({
    queryKey: sloComplianceQueryKey(windowHours),
    queryFn: () =>
      apiFetch(
        `/api/v1/system/slo?window_hours=${windowHours}`,
        { credentials: 'include' },
        (d) => sloComplianceSchema.parse(d),
        10_000,
      ),
    enabled: Boolean(user),
    // Skip tick when a poll is still in-flight — apiFetch uses its own
    // AbortController so overlapping requests can't be cancelled.
    refetchInterval: (q) =>
      q.state.fetchStatus === 'fetching' ? false : REFETCH_INTERVAL_MS,
    staleTime: STALE_TIME_MS,
  });
}
