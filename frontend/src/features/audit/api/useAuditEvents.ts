/**
 * `useAuditEvents` — paginated, filtered global audit-log feed.
 *
 * Mirrors the shape of `useOrders` but hits `/api/v1/audit/events` (A3
 * backend route). The endpoint is root-only; the hook also gates with
 * `enabled: role === 'root'` as defence-in-depth so non-root callers
 * never even fire the fetch.
 *
 * `placeholderData: keepPreviousData` (TanStack Query v5 syntax — the
 * `keepPreviousData` helper is imported from `@tanstack/react-query`)
 * keeps the previous page on screen during pagination clicks so the
 * row body fades rather than flashing a loading skeleton. The audit
 * page is reviewed, not monitored, so `refetchInterval: false` —
 * the user pulls the data, the data doesn't push to them.
 *
 * Date filters: `fromDate` / `toDate` are bare `YYYY-MM-DD` strings
 * from the native `<input type="date">`; the hook widens them to
 * `<from>T00:00:00Z` / `<to>T23:59:59Z` so the server-side `(from, to]`
 * filter treats the input as inclusive UTC days.
 */
import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiFetch } from '@/lib/apiFetch';
import { useCurrentRole } from '@/lib/auth';

import {
  auditEventListResponseSchema,
  type AuditEventListResponse,
  type AuditFiltersState,
} from '../types';

export interface UseAuditEventsParams extends AuditFiltersState {
  page: number;
  pageSize: number;
}

export const auditEventsQueryKey = (params: UseAuditEventsParams) =>
  ['audit', 'events', params] as const;

function buildAuditEventsUrl(params: UseAuditEventsParams): string {
  const qs = new URLSearchParams();
  if (params.actorId) qs.set('actor_id', params.actorId);
  if (params.action) qs.set('action', params.action);
  if (params.resourceType && params.resourceType !== 'other')
    qs.set('resource_type', params.resourceType);
  if (params.fromDate) qs.set('from', `${params.fromDate}T00:00:00Z`);
  if (params.toDate) qs.set('to', `${params.toDate}T23:59:59Z`);
  qs.set('page', String(params.page));
  qs.set('page_size', String(params.pageSize));
  return `/api/v1/audit/events?${qs.toString()}`;
}

export { buildAuditEventsUrl };

export function useAuditEvents(
  params: UseAuditEventsParams,
): UseQueryResult<AuditEventListResponse> {
  const role = useCurrentRole();

  return useQuery<AuditEventListResponse>({
    queryKey: auditEventsQueryKey(params),
    queryFn: () =>
      apiFetch(buildAuditEventsUrl(params), { credentials: 'include' }, (raw) =>
        auditEventListResponseSchema.parse(raw),
      ),
    enabled: role === 'root',
    placeholderData: keepPreviousData,
    refetchInterval: false,
    staleTime: 0,
  });
}
