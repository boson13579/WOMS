import { Navigate, Outlet } from 'react-router-dom';

import { useAuthStore } from '@/features/auth/stores/authStore';

export function ProtectedRoute(): JSX.Element {
  const token = useAuthStore((state) => state.token);

  if (!token) {
    return <Navigate to="/login" replace />;
  }

  return <Outlet />;
}
