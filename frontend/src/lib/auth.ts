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
  return role === 'scheduler' || role === 'root';
}
