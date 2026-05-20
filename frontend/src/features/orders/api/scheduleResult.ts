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

export const scheduleResultKeys = {
  all: ['schedule', 'result'] as const,
};

export function useScheduleResult(): UseQueryResult<ScheduleResult[]> {
  const user = useCurrentUser();
  const role = useCurrentRole();
  const allowed = Boolean(user) && role !== 'viewer';

  return useQuery<ScheduleResult[]>({
    queryKey: scheduleResultKeys.all,
    queryFn: () =>
      apiFetch('/api/v1/schedule/result', { credentials: 'include' }, (d) =>
        scheduleResultResponseSchema.parse(d),
      ),
    enabled: allowed,
    staleTime: 5_000,
  });
}
