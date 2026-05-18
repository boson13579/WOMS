/**
 * `useUsernames` — bulk UUID → username lookup against `/system/usernames`.
 *
 * Available to all authenticated roles (`/system/usernames` is not root-only).
 * Used anywhere the UI needs to resolve a UUID to a display name without
 * granting access to the full `/users` list.
 *
 * Empty input short-circuits to an empty map (no network call) so callers
 * can pass `useUsernames(maybeIds)` without checking length first.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useMemo } from 'react';
import { z } from 'zod';

import { apiFetch } from '@/lib/apiFetch';
import { useCurrentUser } from '@/lib/auth';

const usernamesResponseSchema = z.object({
  usernames: z.record(z.string().nullable()),
});

export type UsernamesMap = Record<string, string | null>;

export const usernamesQueryKey = (ids: readonly string[]) =>
  ['system', 'usernames', [...ids].sort()] as const;

// 1 minute stale time — usernames change rarely
const STALE_TIME_MS = 60_000;

export function useUsernames(ids: readonly string[]): UseQueryResult<UsernamesMap> {
  const user = useCurrentUser();
  const uniqueIds = useMemo(() => Array.from(new Set(ids)), [ids]);
  const queryParam = useMemo(() => uniqueIds.join(','), [uniqueIds]);

  return useQuery<UsernamesMap>({
    queryKey: usernamesQueryKey(uniqueIds),
    queryFn: async () => {
      if (uniqueIds.length === 0) return {};
      const parsed = await apiFetch(
        `/api/v1/system/usernames?ids=${encodeURIComponent(queryParam)}`,
        { credentials: 'include' },
        (d) => usernamesResponseSchema.parse(d),
      );
      return parsed.usernames;
    },
    enabled: Boolean(user),
    staleTime: STALE_TIME_MS,
  });
}
