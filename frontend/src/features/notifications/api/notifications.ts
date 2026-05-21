/**
 * Notifications API client — Zod schemas + React Query hooks.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';

import { apiFetch, jsonHeaders } from '@/lib/apiFetch';
import { useCurrentUser } from '@/lib/auth';

import type { NotificationListResponse, NotificationResponse } from '../types';

// ---------------------------------------------------------------------------
// Zod schemas (runtime validation of API responses)
// ---------------------------------------------------------------------------

const notificationResponseSchema = z.object({
  id: z.string().uuid(),
  user_id: z.string().uuid(),
  order_id: z.string().uuid().nullable(),
  type: z.string(),
  message: z.string(),
  is_read: z.boolean(),
  created_at: z.string(),
});

const notificationListSchema = z.object({
  items: z.array(notificationResponseSchema),
  total: z.number().int(),
});

// ---------------------------------------------------------------------------
// Query key factory
// ---------------------------------------------------------------------------

export const notificationKeys = {
  all: ['notifications'] as const,
  list: (all: boolean) => ['notifications', 'list', { all }] as const,
};

// ---------------------------------------------------------------------------
// React Query hooks
// ---------------------------------------------------------------------------

export interface ListNotificationsParams {
  all?: boolean;
}

export function useNotifications(
  params: ListNotificationsParams = {},
): ReturnType<typeof useQuery<NotificationListResponse>> {
  const user = useCurrentUser();
  const allVal = params.all ?? false;

  return useQuery<NotificationListResponse>({
    queryKey: notificationKeys.list(allVal),
    queryFn: () =>
      apiFetch(`/api/v1/notifications?all=${allVal}`, { credentials: 'include' }, (d) =>
        notificationListSchema.parse(d),
      ),
    enabled: Boolean(user),
  });
}

export function useMarkNotificationRead(): ReturnType<
  typeof useMutation<NotificationResponse, Error, string>
> {
  const qc = useQueryClient();

  return useMutation<NotificationResponse, Error, string>({
    mutationFn: (id) =>
      apiFetch(
        `/api/v1/notifications/${id}/read`,
        {
          method: 'PATCH',
          credentials: 'include',
          headers: jsonHeaders(),
        },
        (d) => notificationResponseSchema.parse(d),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: notificationKeys.all });
    },
  });
}

export function useMarkAllNotificationsRead(): ReturnType<
  typeof useMutation<{ updated: number }, Error, void>
> {
  const qc = useQueryClient();

  return useMutation<{ updated: number }>({
    mutationFn: () =>
      apiFetch(
        '/api/v1/notifications/read-all',
        {
          method: 'PATCH',
          credentials: 'include',
          headers: jsonHeaders(),
        },
        (d) => z.object({ updated: z.number().int() }).parse(d),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: notificationKeys.all });
    },
  });
}
