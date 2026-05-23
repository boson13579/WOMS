/**
 * `useScheduleStatus` — scheduler lifecycle snapshot for the dashboard badge.
 *
 * Polls `GET /api/v1/schedule/status` every 2 s so the badge surfaces
 * state flips (idle → running, running → failed) within a second of
 * the worker writing them. `useDashboardWs` also invalidates this query
 * on `schedule.compound_accepted` / `schedule.compound_failed` events
 * for instant updates when those fire — polling is the safety net for
 * states that don't emit an event. Permission `order_manager+`
 * (backend enforces; the dashboard hides the widget for viewer anyway).
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { z } from 'zod';

import { useCurrentUser, useCurrentRole } from '@/lib/auth';

import type { ScheduleStatusResponse } from '../types';

import { apiFetch } from './apiFetch';

const scheduleStatusResponseSchema = z.object({
  state: z.enum(['idle', 'running', 'failed']),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  task_id: z.string().nullable(),
  error: z.string().nullable(),
  message: z.string().nullable(),
});

export const scheduleStatusQueryKey = ['schedule', 'status'] as const;

const REFETCH_INTERVAL_MS = 2_000;

export function useScheduleStatus(): UseQueryResult<ScheduleStatusResponse> {
  const user = useCurrentUser();
  const role = useCurrentRole();
  // ``viewer`` is blocked at the backend (403). The dashboard never mounts
  // this hook for viewer, but defending against it here means a stray
  // mount won't pollute logs with 403s.
  const allowed = Boolean(user) && role !== 'viewer';

  return useQuery<ScheduleStatusResponse>({
    queryKey: scheduleStatusQueryKey,
    queryFn: () =>
      apiFetch('/api/v1/schedule/status', { credentials: 'include' }, (d) =>
        scheduleStatusResponseSchema.parse(d),
      ),
    enabled: allowed,
    refetchInterval: REFETCH_INTERVAL_MS,
    staleTime: 1_000,
  });
}
