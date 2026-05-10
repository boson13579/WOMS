import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';

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
  if (!res.ok) return [];
  return usersResponseSchema.parse(await res.json()).users;
}

export function useUsers(): UserOption[] {
  const { data } = useQuery<UserOption[]>({
    queryKey: ['users'],
    queryFn: fetchUsers,
    staleTime: 5 * 60 * 1000,
  });
  return data ?? [];
}
