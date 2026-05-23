/**
 * `useSystemHealth` — fetches the dashboard's Service Health card data.
 *
 * Backed by `GET /api/v1/system/health` (open to any logged-in user,
 * viewers included). Polls every 2s so operators see service-state
 * transitions (e.g. Redis going down, Celery worker pool shrinking)
 * within seconds — needed for the demo / debug flow. Backend cost is
 * negligible at single-digit viewer count: each probe is ~50ms server
 * side, no DB writes.
 *
 * Future: when the WS-cookie-auth follow-up PR lands (see
 * `notes/ws-design-spec.md`), wire `schedule.updated` / `schedule.materialized`
 * to invalidate this query — but the polling stays as the safety net.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { z } from 'zod';

import { useCurrentUser } from '@/lib/auth';

import type { SystemHealthResponse } from '../types';

import { apiFetch } from './apiFetch';

// ---------------------------------------------------------------------------
// Zod schema — runtime contract with the backend
// ---------------------------------------------------------------------------

const serviceStatusSchema = z.enum(['healthy', 'warning', 'error']);

const serviceHealthDetailSchema = z.object({
  label: z.string(),
  value: z.string(),
});

const serviceHealthEntrySchema = z.object({
  id: z.enum(['api', 'postgres', 'redis', 'celery']),
  name: z.string(),
  status: serviceStatusSchema,
  summary: z.string(),
  details: z.array(serviceHealthDetailSchema),
});

const systemHealthResponseSchema = z.object({
  services: z.array(serviceHealthEntrySchema),
});

// ---------------------------------------------------------------------------
// Query key + hook
// ---------------------------------------------------------------------------

export const systemHealthQueryKey = ['system', 'health'] as const;

const REFETCH_INTERVAL_MS = 2_000;

export function useSystemHealth(): UseQueryResult<SystemHealthResponse> {
  const user = useCurrentUser();

  return useQuery<SystemHealthResponse>({
    queryKey: systemHealthQueryKey,
    queryFn: () =>
      apiFetch(
        '/api/v1/system/health',
        { credentials: 'include' },
        (d) => systemHealthResponseSchema.parse(d),
        // Bumped over the apiFetch default (5s) because this endpoint
        // is *itself* a probe — its job is to report degraded services,
        // and individual probes can legitimately take 2–3s each when a
        // dependency is unreachable. 10s gives the backend room to
        // collect a full per-service status payload without the client
        // giving up first.
        10_000,
      ),
    enabled: Boolean(user),
    refetchInterval: REFETCH_INTERVAL_MS,
    staleTime: 1_000,
  });
}
