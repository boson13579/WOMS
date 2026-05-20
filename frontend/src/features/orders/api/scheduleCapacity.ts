import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { z } from 'zod';

import { apiFetch } from '@/lib/apiFetch';
import { useCurrentRole, useCurrentUser } from '@/lib/auth';

const capacityEntrySchema = z.object({
  date: z.string(),
  cumulative_remaining: z.number().int().nonnegative(),
});

const scheduleCapacitySchema = z.object({
  base_date: z.string(),
  daily_capacity: z.number().int().positive(),
  entries: z.array(capacityEntrySchema),
});

export interface ScheduleCapacityEntry {
  date: string;
  cumulative_remaining: number;
}

export interface ScheduleCapacity {
  base_date: string;
  daily_capacity: number;
  entries: ScheduleCapacityEntry[];
}

export interface DailyCapacity {
  date: string;
  remaining: number;
  dailyCapacity: number;
}

export const scheduleCapacityKeys = {
  all: ['schedule', 'capacity'] as const,
};

export function toDailyCapacity(capacity: ScheduleCapacity): DailyCapacity[] {
  return capacity.entries.map((entry, index) => {
    const previous = index === 0 ? 0 : capacity.entries[index - 1].cumulative_remaining;
    return {
      date: entry.date,
      remaining: entry.cumulative_remaining - previous,
      dailyCapacity: capacity.daily_capacity,
    };
  });
}

export function useScheduleCapacity(): UseQueryResult<ScheduleCapacity> {
  const user = useCurrentUser();
  const role = useCurrentRole();
  const allowed = Boolean(user) && role !== 'viewer';

  return useQuery<ScheduleCapacity>({
    queryKey: scheduleCapacityKeys.all,
    queryFn: () =>
      apiFetch('/api/v1/schedule/capacity', { credentials: 'include' }, (d) =>
        scheduleCapacitySchema.parse(d),
      ),
    enabled: allowed,
    staleTime: 5_000,
  });
}
