/**
 * Application route map.
 *
 * The whole tree lives inside a pathless ``SessionBoundary`` so the
 * global 401 handler can be installed once with access to both the
 * QueryClient (cleared on every 401) and the router (used to navigate
 * to ``/login?next=…``). Protected feature pages share the AppShell
 * layout; auth pages live outside that shell but inside an
 * ``AuthOnlyRoute`` so authed users hitting /login bounce home.
 */
import { createBrowserRouter, type RouteObject } from 'react-router-dom';

import { AppShell } from '@/components/layout/AppShell';
import { AuthOnlyRoute } from '@/components/layout/AuthOnlyRoute';
import { ProtectedRoute } from '@/components/layout/ProtectedRoute';
import { RoleProtectedRoute } from '@/components/layout/RoleProtectedRoute';
import { SessionBoundary } from '@/components/layout/SessionBoundary';
import { AuthPage } from '@/features/auth/components/AuthPage';
import { DashboardPage } from '@/features/dashboard/components/DashboardPage';
import { OrdersPage } from '@/features/orders/components/OrdersPage';
import { AdminUsersPage } from '@/features/users/components/AdminUsersPage';

export const routes: RouteObject[] = [
  {
    element: <SessionBoundary />,
    children: [
      {
        path: '/',
        element: <ProtectedRoute />,
        children: [
          {
            element: <AppShell />,
            children: [
              { index: true, element: <DashboardPage /> },
              { path: 'orders', element: <OrdersPage /> },
              {
                // Root-only nested group. Layout-route shape so future
                // root-only routes plug in here without per-page wrapping.
                element: <RoleProtectedRoute allowedRoles={['root']} />,
                children: [{ path: 'users', element: <AdminUsersPage /> }],
              },
              // Future feature routes go here:
              // { path: 'scheduling', element: <SchedulingPage /> },
            ],
          },
        ],
      },
      {
        path: '/login',
        element: (
          <AuthOnlyRoute>
            <AuthPage />
          </AuthOnlyRoute>
        ),
      },
      {
        path: '/register',
        element: (
          <AuthOnlyRoute>
            <AuthPage />
          </AuthOnlyRoute>
        ),
      },
    ],
  },
];

export const router = createBrowserRouter(routes);
