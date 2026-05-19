import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { getMe, type MeResponse } from '@/features/auth/api/auth';
import { useAuthStore } from '@/features/auth/stores/authStore';

export type UserRole = 'root' | 'scheduler' | 'order_manager' | 'viewer';

export function useCurrentUser() {
  return useAuthStore((s) => s.user);
}

export function useCurrentRole(): UserRole | null {
  const user = useAuthStore((s) => s.user);
  return user ? (user.role as UserRole) : null;
}

export function useCurrentUserId(): string | null {
  const user = useAuthStore((s) => s.user);
  return user ? user.id : null;
}

export function useCanWrite(): boolean {
  const role = useCurrentRole();
  return role === 'root' || role === 'scheduler' || role === 'order_manager';
}

export function useCanSchedule(): boolean {
  const role = useCurrentRole();
  return role === 'root' || role === 'scheduler';
}

/**
 * Server-confirmed identity for the current session.
 *
 * Backed by ``GET /api/v1/auth/me`` and cached for 30 s. Refetches on
 * window focus so a role downgrade or deactivation server-side propagates
 * within one focus cycle without manual page reloads. ``retry: false`` so
 * a 401 fails fast and the global ``unauthorizedHandler`` in ``apiFetch``
 * can clear the cache and redirect to ``/login?next=…`` immediately.
 *
 * The hook does NOT attempt to logout on non-401 errors; sustained 5xx
 * is rendered as an error-state UI in ``ProtectedRoute`` with Retry +
 * Sign out actions.
 *
 * ``enabled`` defaults to ``true`` so callers don't have to pass anything
 * for the common case. ``ProtectedRoute`` passes the store's ``user``
 * boolean so the query stops firing the moment the user logs out (which
 * also keeps the Rules of Hooks intact — the hook always runs, just
 * idles when disabled).
 */
export function useMe(options: { enabled?: boolean } = {}): UseQueryResult<MeResponse> {
  return useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: getMe,
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    retry: false,
    enabled: options.enabled ?? true,
  });
}
