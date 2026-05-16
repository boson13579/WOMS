import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';

import { useCurrentRole } from '@/lib/auth';

const userOptionSchema = z.object({
  id: z.string().uuid(),
  username: z.string(),
  email: z.string().nullable(),
});

const usersResponseSchema = z.object({
  users: z.array(userOptionSchema),
});

export type UserOption = z.infer<typeof userOptionSchema>;

async function fetchUsers(): Promise<UserOption[]> {
  const res = await fetch('/api/v1/users', { credentials: 'include' });
  if (!res.ok) throw new Error(String(res.status));
  return usersResponseSchema.parse(await res.json()).users;
}

const EMPTY_USERS: UserOption[] = [];

/**
 * Root-only listing of all users. Gated on role so non-root sessions don't
 * hammer `/api/v1/users` with 401s that just resolve to the same empty array.
 * Callers that need name lookups for any logged-in user should use
 * `useUsernames` from the dashboard feature instead.
 */
export function useUsers(): UserOption[] {
  const role = useCurrentRole();
  const { data } = useQuery<UserOption[]>({
    queryKey: ['users'],
    queryFn: fetchUsers,
    staleTime: 5 * 60 * 1000,
    retry: false,
    enabled: role === 'root',
  });
  return data ?? EMPTY_USERS;
}
