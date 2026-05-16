/**
 * Application route map.
 *
 * Protected feature pages share the AppShell layout. Auth pages live outside
 * that shell so login and registration keep their full-screen layout.
 */
import { createBrowserRouter, type RouteObject } from 'react-router-dom';

import { AppShell } from '@/components/layout/AppShell';
import { ProtectedRoute } from '@/components/layout/ProtectedRoute';
import { AuthPage } from '@/features/auth/components/AuthPage';
import { DashboardPage } from '@/features/dashboard/components/DashboardPage';
import { OrdersPage } from '@/features/orders/components/OrdersPage';
import { AdminUsersPage } from '@/features/users/components/AdminUsersPage';

export const routes: RouteObject[] = [
  {
    path: '/',
    element: <ProtectedRoute />,
    children: [
      {
        element: <AppShell />,
        children: [
          { index: true, element: <DashboardPage /> },
          { path: 'orders', element: <OrdersPage /> },
          { path: 'users', element: <AdminUsersPage /> },
          // Future feature routes go here:
          // { path: 'scheduling', element: <SchedulingPage /> },
        ],
      },
    ],
  },
  // Auth pages live outside the AppShell so they have their own full-screen layout.
  { path: '/login', element: <AuthPage /> },
  { path: '/register', element: <AuthPage /> },
];

export const router = createBrowserRouter(routes);
