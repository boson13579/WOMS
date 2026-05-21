/**
 * `useAuditActions` — distinct audit `action` strings present in the DB.
 *
 * Backs the Action filter combobox on the admin audit page. The
 * underlying endpoint `GET /api/v1/audit/actions` returns the
 * ASC-sorted, distinct set of action values currently in
 * `audit_logs`, so the typeahead always matches reality (no more
 * drift from a hard-coded constant).
 *
 * RBAC: root-only on the server. The hook also gates with
 * `enabled: role === 'root'` as defence-in-depth so non-root callers
 * never fire the fetch. `staleTime: 60_000` debounces repeat calls
 * within a page session — the action set turns over slowly enough
 * that one minute of cache is appropriate.
 *
 * Empty list (not error) when `audit_logs` is empty; the combobox
 * renders an empty dropdown with a free-text fallback in that case.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { z } from 'zod';

import { apiFetch } from '@/lib/apiFetch';
import { useCurrentRole } from '@/lib/auth';

export const auditActionsResponseSchema = z.object({
  actions: z.array(z.string()),
});

export type AuditActionsResponse = z.infer<typeof auditActionsResponseSchema>;

export const auditActionsQueryKey = ['audit', 'actions'] as const;

export function useAuditActions(): UseQueryResult<AuditActionsResponse> {
  const role = useCurrentRole();

  return useQuery<AuditActionsResponse>({
    queryKey: auditActionsQueryKey,
    queryFn: () =>
      apiFetch('/api/v1/audit/actions', { credentials: 'include' }, (raw) =>
        auditActionsResponseSchema.parse(raw),
      ),
    enabled: role === 'root',
    staleTime: 60_000,
    refetchInterval: false,
  });
}
