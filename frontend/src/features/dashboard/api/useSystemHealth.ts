/**
 * `useSystemHealth` — fetches the dashboard's Service Health card data.
 *
 * Backed by `GET /api/v1/system/health` (open to any logged-in user,
 * viewers included). The polling cadence is intentionally conservative —
 * service health changes slowly and dashboard re-renders cost CSS work
 * across the entire page. A 30-second cadence is well below MTTR for
 * the underlying services and well above the cost-of-render budget.
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

const REFETCH_INTERVAL_MS = 30_000;

export function useSystemHealth(): UseQueryResult<SystemHealthResponse> {
  const user = useCurrentUser();

  return useQuery<SystemHealthResponse>({
    queryKey: systemHealthQueryKey,
    queryFn: () =>
      apiFetch('/api/v1/system/health', { credentials: 'include' }, (d) =>
        systemHealthResponseSchema.parse(d),
      ),
    enabled: Boolean(user),
    refetchInterval: REFETCH_INTERVAL_MS,
    staleTime: 10_000,
  });
}
