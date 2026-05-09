import { Navigate, Outlet } from 'react-router-dom';

import { useAuthStore } from '@/features/auth/stores/authStore';

export function ProtectedRoute(): JSX.Element {
  const user = useAuthStore((state) => state.user);
  const expiresAt = useAuthStore((state) => state.expiresAt);

  if (!user || !expiresAt || expiresAt <= Date.now()) {
    return <Navigate to="/login" replace />;
  }

  return <Outlet />;
}
