/**
 * `usePendingOps` — list of queued compounds with their drain rank.
 *
 * The endpoint returns the **entire** queue (can be 1000+ entries) — the
 * Pending Ops widget renders only the first N rows + a "showing X of
 * total" footer. The slicing is the caller's responsibility so the
 * unsliced data is still available if a future expanded-view UI lands.
 *
 * Polled every 10 s because the queue is highly dynamic (a single
 * batch PATCH can push dozens of compounds in seconds).
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { z } from 'zod';

import { useCurrentUser, useCurrentRole } from '@/lib/auth';

import type { PendingOpsEntry } from '../types';

import { apiFetch } from './apiFetch';

const pendingOpsOpViewSchema = z.object({
  op: z.enum(['add', 'remove', 'pin', 'unpin']),
  order_id: z.string().uuid(),
  order_number: z.string(),
});

const pendingOpsEntrySchema = z.object({
  compound_id: z.string().uuid(),
  rank: z.number().int().min(1),
  group: z.enum(['shrink', 'grow']),
  op_count: z.number().int().min(1),
  ops: z.array(pendingOpsOpViewSchema),
  requested_by: z.string().uuid(),
});

const pendingOpsResponseSchema = z.array(pendingOpsEntrySchema);

export const pendingOpsQueryKey = ['schedule', 'pending-ops'] as const;

const REFETCH_INTERVAL_MS = 10_000;

export function usePendingOps(): UseQueryResult<PendingOpsEntry[]> {
  const user = useCurrentUser();
  const role = useCurrentRole();
  const allowed = Boolean(user) && role !== 'viewer';

  return useQuery<PendingOpsEntry[]>({
    queryKey: pendingOpsQueryKey,
    queryFn: () =>
      apiFetch('/api/v1/schedule/pending-ops', { credentials: 'include' }, (d) =>
        pendingOpsResponseSchema.parse(d),
      ),
    enabled: allowed,
    refetchInterval: REFETCH_INTERVAL_MS,
    staleTime: 5_000,
  });
}
