/**
 * useRedMetrics — fetches RED (rate / errors / duration) aggregates.
 *
 * Backed by ``GET /api/v1/system/red?window_seconds=…``. The endpoint
 * returns a point-in-time aggregate, so on every successful poll we
 * also push the headline metrics into ``useRedHistoryStore`` so the
 * sparklines have something to render. 2-second polling lets the
 * sparkline feel live during demos / bombard runs without putting real
 * load on the backend (each call is a Redis ZRANGEBYSCORE + aggregate,
 * ~10-30ms server-side).
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

const REFETCH_INTERVAL_MS = 2_000;
const STALE_TIME_MS = 1_000;

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
    // Skip tick when a poll is still in-flight — apiFetch uses its own
    // AbortController so overlapping requests can't be cancelled.
    refetchInterval: (q) =>
      q.state.fetchStatus === 'fetching' ? false : REFETCH_INTERVAL_MS,
    staleTime: STALE_TIME_MS,
  });

  // Append-on-success ring-buffer push. React Query v5 removed the
  // ``onSuccess`` option, so we mirror it via a tiny ``useEffect``
  // watching ``dataUpdatedAt`` — fires once per successful poll
  // regardless of whether the value changed.
  //
  // Skip when ``data_status === 'degraded'``: backend returns all-zero
  // samples in this state (Redis unreachable). Pushing those zeros into
  // the ring buffer would draw the sparkline curving down to 0, making
  // a metrics-availability outage look like a real traffic collapse.
  // Freezing the buffer keeps the last known good trend visible so an
  // operator can see what was happening just before visibility was
  // lost; the page-level degraded banner already communicates "data
  // unavailable" textually.
  const { data, dataUpdatedAt } = query;
  useEffect(() => {
    if (data && data.data_status !== 'degraded') {
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
