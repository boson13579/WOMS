import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { z } from 'zod';

import { apiFetch } from '@/lib/apiFetch';
import { useCurrentRole, useCurrentUser } from '@/lib/auth';

import type { ScheduleResult } from '../types';

const dailyAssignmentSchema = z.object({
  date: z.string(),
  quantity: z.number().int(),
});

const scheduleResultSchema = z.object({
  id: z.string().uuid(),
  order_number: z.string(),
  customer_name: z.string(),
  wafer_quantity: z.number().int(),
  requested_delivery_date: z.string(),
  scheduled_production_date: z.string().nullable(),
  expected_delivery_date: z.string().nullable(),
  status: z.enum(['pending', 'scheduled', 'in_production', 'completed', 'cancelled']),
  daily_breakdown: z.array(dailyAssignmentSchema),
});

const scheduleResultResponseSchema = z.array(scheduleResultSchema);

interface ScheduleResultParams {
  // When true, the response also includes ``completed`` orders within the
  // server-side rolling window (today − 30 days by default; the backend
  // owns the actual lower bound). When false / omitted, only
  // ``scheduled`` + ``in_production`` are returned — matches the legacy
  // contract that pre-completed-on-calendar callers rely on.
  includeCompleted?: boolean;
}

export const scheduleResultKeys = {
  // 'all' targets every variant of this query — invalidating it (e.g.
  // after a PATCH/cancel/delete commits) refreshes both the
  // include_completed=true and include_completed=false versions if both
  // happen to be cached. The variant-specific key is appended for the
  // actual fetch so React Query caches them separately.
  all: ['schedule', 'result'] as const,
  variant: (params: ScheduleResultParams) =>
    ['schedule', 'result', { includeCompleted: params.includeCompleted ?? false }] as const,
};

export function useScheduleResult(
  params: ScheduleResultParams = {},
): UseQueryResult<ScheduleResult[]> {
  const user = useCurrentUser();
  const role = useCurrentRole();
  const allowed = Boolean(user) && role !== 'viewer';
  const includeCompleted = params.includeCompleted ?? false;

  const url = includeCompleted
    ? '/api/v1/schedule/result?include_completed=true'
    : '/api/v1/schedule/result';

  return useQuery<ScheduleResult[]>({
    queryKey: scheduleResultKeys.variant({ includeCompleted }),
    queryFn: () =>
      apiFetch(url, { credentials: 'include' }, (d) => scheduleResultResponseSchema.parse(d)),
    enabled: allowed,
    staleTime: 5_000,
  });
}
