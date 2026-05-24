import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { z } from 'zod';

import { apiFetch } from '@/lib/apiFetch';
import { useCurrentRole, useCurrentUser } from '@/lib/auth';

const capacityUsageEntrySchema = z.object({
  date: z.string(),
  used: z.number().int().nonnegative(),
  remaining: z.number().int().nonnegative(),
});

const scheduleCapacityUsageSchema = z.object({
  base_date: z.string(),
  daily_capacity: z.number().int().positive(),
  entries: z.array(capacityUsageEntrySchema),
});

export interface ScheduleCapacityEntry {
  date: string;
  used: number;
  remaining: number;
}

export interface ScheduleCapacity {
  base_date: string;
  daily_capacity: number;
  entries: ScheduleCapacityEntry[];
}

export interface DailyCapacity {
  date: string;
  used: number;
  remaining: number;
  dailyCapacity: number;
}

export const scheduleCapacityKeys = {
  all: ['schedule', 'capacity-usage'] as const,
};

export function toDailyCapacity(capacity: ScheduleCapacity): DailyCapacity[] {
  return capacity.entries.map((entry) => ({
    date: entry.date,
    used: entry.used,
    remaining: entry.remaining,
    dailyCapacity: capacity.daily_capacity,
  }));
}

export function useScheduleCapacity(): UseQueryResult<ScheduleCapacity> {
  const user = useCurrentUser();
  const role = useCurrentRole();
  const allowed = Boolean(user) && role !== 'viewer';

  return useQuery<ScheduleCapacity>({
    queryKey: scheduleCapacityKeys.all,
    queryFn: () =>
      apiFetch('/api/v1/schedule/capacity-usage', { credentials: 'include' }, (d) =>
        scheduleCapacityUsageSchema.parse(d),
      ),
    enabled: allowed,
    staleTime: 5_000,
  });
}
