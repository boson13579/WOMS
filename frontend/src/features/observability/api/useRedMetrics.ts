/**
 * useRedMetrics — fetches RED (rate / errors / duration) aggregates.
 *
 * Backed by ``GET /api/v1/system/red?window_seconds=…``. The endpoint
 * returns a point-in-time aggregate, so on every successful poll we
 * also push the headline metrics into ``useRedHistoryStore`` so the
 * sparklines have something to render. 10-second polling matches the
 * cadence the operator-grade card needs: fast enough to see a spike
 * unfold, slow enough to keep server cost negligible.
 *
 * The query key includes ``window_seconds`` so changing the time-range
 * pill triggers a new fetch (each window is cached independently by
 * React Query rather than thrashing one cache slot).
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useEffect } from 'react';

import { apiFetch } from '@/lib/apiFetch';
import { useCurrentUser } from '@/lib/auth';

import { useRedHistoryStore } from '../stores/redHistoryStore';
import { redMetricsResponseSchema, type RedMetricsResponse } from '../types';

const REFETCH_INTERVAL_MS = 10_000;
const STALE_TIME_MS = 5_000;

export function redMetricsQueryKey(windowSeconds: number): readonly unknown[] {
  return ['system', 'red', windowSeconds] as const;
}

export function useRedMetrics(windowSeconds: number): UseQueryResult<RedMetricsResponse> {
  const user = useCurrentUser();
  const pushHistory = useRedHistoryStore((s) => s.push);

  const query = useQuery<RedMetricsResponse>({
    queryKey: redMetricsQueryKey(windowSeconds),
    queryFn: () =>
      apiFetch(
        `/api/v1/system/red?window_seconds=${windowSeconds}`,
        { credentials: 'include' },
        (d) => redMetricsResponseSchema.parse(d),
        10_000,
      ),
    enabled: Boolean(user),
    refetchInterval: REFETCH_INTERVAL_MS,
    staleTime: STALE_TIME_MS,
  });

  // Append-on-success ring-buffer push. React Query v5 removed the
  // ``onSuccess`` option, so we mirror it via a tiny ``useEffect``
  // watching ``dataUpdatedAt`` — fires once per successful poll
  // regardless of whether the value changed.
  const { data, dataUpdatedAt } = query;
  useEffect(() => {
    if (data) {
      pushHistory({
        rate: data.rate_per_sec,
        errorPct: data.error_pct,
        p95: data.latency_ms.p95,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataUpdatedAt]);

  return query;
}
