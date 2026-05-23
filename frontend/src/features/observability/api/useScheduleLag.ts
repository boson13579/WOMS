/**
 * useScheduleLag — fetches the schedule-pipeline lag aggregate.
 *
 * Backed by ``GET /api/v1/system/schedule-lag?window_seconds=…``. Each
 * successful poll pushes the P95 into ``useRedHistoryStore`` so the KPI
 * card (which replaces the old SLO slot) can render a sparkline aligned
 * with the other RED cards.
 *
 * 2-second polling matches ``useRedMetrics`` so the four KPI cards
 * refresh together — important for the eye to read "Rate up, Lag up" as
 * one coherent state change rather than staggered noise.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useEffect } from 'react';

import { apiFetch } from '@/lib/apiFetch';
import { useCurrentUser } from '@/lib/auth';

import { useRedHistoryStore } from '../stores/redHistoryStore';
import { scheduleLagSchema, type ScheduleLag } from '../types';

const REFETCH_INTERVAL_MS = 2_000;
const STALE_TIME_MS = 1_000;

export function scheduleLagQueryKey(windowSeconds: number): readonly unknown[] {
  return ['system', 'schedule-lag', windowSeconds] as const;
}

export function useScheduleLag(windowSeconds = 60): UseQueryResult<ScheduleLag> {
  const user = useCurrentUser();
  const pushHistory = useRedHistoryStore((s) => s.push);

  const query = useQuery<ScheduleLag>({
    queryKey: scheduleLagQueryKey(windowSeconds),
    queryFn: () =>
      apiFetch(
        `/api/v1/system/schedule-lag?window_seconds=${windowSeconds}`,
        { credentials: 'include' },
        (d) => scheduleLagSchema.parse(d),
        10_000,
      ),
    enabled: Boolean(user),
    refetchInterval: (q) => (q.state.fetchStatus === 'fetching' ? false : REFETCH_INTERVAL_MS),
    staleTime: STALE_TIME_MS,
  });

  const { data, dataUpdatedAt } = query;
  useEffect(() => {
    // Skip empty windows (no samples yet) — pushing 0 would render a
    // misleading flat line at zero before the first compound commits.
    if (data && data.sample_count > 0) {
      pushHistory({ lagP95: data.p95_ms });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataUpdatedAt]);

  return query;
}
