/**
 * useUseResources — fetches DB-pool / Redis / Celery resource snapshot.
 *
 * Backed by ``GET /api/v1/system/resources``. Each section
 * (``db_pool`` / ``redis`` / ``celery``) is independently nullable: a
 * single probe failure produces ``null`` for that section while the
 * other two still render. The frontend treats ``null`` as "we have no
 * signal, show a degraded card" rather than as a request-level error.
 *
 * 3-second polling — fast enough to catch a Redis / Celery degradation
 * during demos but well above the per-probe cost (Celery ``inspect()``
 * + Redis ``INFO`` add up to ~100ms server-side, dominant cost in this
 * hook). RED uses 2s; this stays slower because USE probes are heavier.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiFetch } from '@/lib/apiFetch';
import { useCurrentUser } from '@/lib/auth';

import { useResourcesSchema, type UseResources } from '../types';

const REFETCH_INTERVAL_MS = 3_000;
const STALE_TIME_MS = 1_500;

export const useResourcesQueryKey = ['system', 'resources'] as const;

export function useUseResources(): UseQueryResult<UseResources> {
  const user = useCurrentUser();

  return useQuery<UseResources>({
    queryKey: useResourcesQueryKey,
    queryFn: () =>
      apiFetch(
        '/api/v1/system/resources',
        { credentials: 'include' },
        (d) => useResourcesSchema.parse(d),
        10_000,
      ),
    enabled: Boolean(user),
    refetchInterval: REFETCH_INTERVAL_MS,
    staleTime: STALE_TIME_MS,
  });
}
