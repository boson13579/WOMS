/**
 * `useScheduleCapacity` — 30-day prefix-sum series for the dashboard chart.
 *
 * Polls `GET /api/v1/schedule/capacity` every 30 s. Capacity is an
 * algorithm-internal quantity that lives only in Redis, so the value
 * effectively changes only when `materialize_schedule_task` or
 * `advance_day_task` runs — 30 s is well below the upper bound on
 * change frequency.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { z } from 'zod';

import { useCurrentUser, useCurrentRole } from '@/lib/auth';

import type { ScheduleCapacityResponse } from '../types';

import { apiFetch } from './apiFetch';

const capacityEntrySchema = z.object({
  date: z.string(),
  cumulative_remaining: z.number().int().min(0),
});

const scheduleCapacityResponseSchema = z.object({
  base_date: z.string(),
  daily_capacity: z.number().int().positive(),
  entries: z.array(capacityEntrySchema),
});

export const scheduleCapacityQueryKey = ['schedule', 'capacity'] as const;

const REFETCH_INTERVAL_MS = 30_000;

export function useScheduleCapacity(): UseQueryResult<ScheduleCapacityResponse> {
  const user = useCurrentUser();
  const role = useCurrentRole();
  const allowed = Boolean(user) && role !== 'viewer';

  return useQuery<ScheduleCapacityResponse>({
    queryKey: scheduleCapacityQueryKey,
    queryFn: () =>
      apiFetch('/api/v1/schedule/capacity', { credentials: 'include' }, (d) =>
        scheduleCapacityResponseSchema.parse(d),
      ),
    enabled: allowed,
    refetchInterval: REFETCH_INTERVAL_MS,
    staleTime: 15_000,
  });
}
