/**
 * Application route map.
 *
 * Phase 1 ships only two routes: the dashboard (default) and a placeholder
 * login page. Phase 2 will add nested routes under `/orders`, `/scheduling`,
 * etc. — keeping the AppShell layout consistent.
 */
import { createBrowserRouter } from 'react-router-dom';

import { AppShell } from '@/components/layout/AppShell';
import { LoginPage } from '@/features/auth/components/LoginPage';
import { DashboardPage } from '@/features/dashboard/components/DashboardPage';

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <DashboardPage /> },
      // Phase 2 stubs go here:
      // { path: 'orders', element: <OrdersPage /> },
      // { path: 'scheduling', element: <SchedulingPage /> },
    ],
  },
  // Login lives outside the AppShell so it has its own centered layout.
  { path: '/login', element: <LoginPage /> },
]);
