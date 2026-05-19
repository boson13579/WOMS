/**
 * Role gate sibling of ``ProtectedRoute``.
 *
 * Wraps a nested route group under the protected app shell and renders
 * ``<Outlet />`` only when ``useCurrentRole()`` returns a role in the
 * ``allowedRoles`` list. Non-allowed roles (and the defensive ``null``
 * case where ``ProtectedRoute`` would normally have short-circuited)
 * are redirected to ``/`` instead of seeing the page chrome flash before
 * a backend 403.
 *
 * Layout route shape (``element`` + ``children``) chosen so additional
 * root-only routes can be added under a single gate without per-page
 * duplication. The redirect target is the dashboard; we deliberately
 * skip a 403 page to keep the surface area minimal. Backend remains the
 * source of truth — this gate is UX polish, not a security boundary.
 */
import { Navigate, Outlet } from 'react-router-dom';

import { useCurrentRole, type UserRole } from '@/lib/auth';

interface RoleProtectedRouteProps {
  allowedRoles: UserRole[];
}

export function RoleProtectedRoute({ allowedRoles }: RoleProtectedRouteProps): JSX.Element {
  const role = useCurrentRole();

  if (role === null || !allowedRoles.includes(role)) {
    return <Navigate to="/" replace />;
  }

  return <Outlet />;
}
