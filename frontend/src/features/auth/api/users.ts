import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';

import { apiFetch } from '@/lib/apiFetch';
import { useCanWrite, useCurrentRole, useCurrentUser } from '@/lib/auth';

const userOptionSchema = z.object({
  id: z.string().uuid(),
  username: z.string(),
  email: z.string().nullable(),
});

const usersResponseSchema = z.object({
  users: z.array(userOptionSchema),
});

const assignableUsersResponseSchema = z.array(userOptionSchema);

export type UserOption = z.infer<typeof userOptionSchema>;

async function fetchUsers(): Promise<UserOption[]> {
  const parsed = await apiFetch('/api/v1/users', { credentials: 'include' }, (raw) =>
    usersResponseSchema.parse(raw),
  );
  return parsed.users;
}

async function fetchAssignableUsers(): Promise<UserOption[]> {
  return apiFetch('/api/v1/users/assignable', { credentials: 'include' }, (raw) =>
    assignableUsersResponseSchema.parse(raw),
  );
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

/**
 * Users the current role can assign as order owners.
 *
 * Backend contract: order_manager receives only themselves; scheduler/root
 * receive active users. Viewers cannot create orders, so this query stays
 * disabled for them.
 */
export function useAssignableUsers(): UserOption[] {
  const user = useCurrentUser();
  const canAssign = useCanWrite();
  const { data } = useQuery<UserOption[]>({
    queryKey: ['users', 'assignable', user?.id],
    queryFn: fetchAssignableUsers,
    staleTime: 5 * 60 * 1000,
    retry: false,
    enabled: canAssign,
  });
  return data ?? EMPTY_USERS;
}
