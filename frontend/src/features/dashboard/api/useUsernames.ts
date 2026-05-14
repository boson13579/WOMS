/**
 * `useUsernames` — bulk UUID → username lookup against `/system/usernames`.
 *
 * Used by the Pending Ops table to render the requester's username next
 * to each compound. `/users` is root-only, but a name-only lookup is OK
 * to expose to any logged-in user, so dashboard widgets that need to
 * label "who did this?" pull from here.
 *
 * Empty input short-circuits to an empty map (no network call) so the
 * caller can pass `useUsernames(maybeIds)` without checking length.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useMemo } from 'react';
import { z } from 'zod';

import { useCurrentUser } from '@/lib/auth';

import { apiFetch } from './apiFetch';

const usernamesResponseSchema = z.object({
  usernames: z.record(z.string().nullable()),
});

export type UsernamesMap = Record<string, string | null>;

export const usernamesQueryKey = (ids: readonly string[]) =>
  ['system', 'usernames', [...ids].sort()] as const;

// 1 minute stale time — usernames change rarely (renames are uncommon)
// and many compounds in one queue share a few requesters, so reusing
// the cache across renders of Pending Ops keeps the network quiet.
const STALE_TIME_MS = 60_000;

export function useUsernames(ids: readonly string[]): UseQueryResult<UsernamesMap> {
  const user = useCurrentUser();
  // Dedup while preserving stability for the React Query key. Pending
  // Ops polls every 10s and re-renders siblings, so memoizing keeps
  // the Set construction off the hot path.
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
