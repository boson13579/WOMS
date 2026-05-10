/**
 * Application route map.
 *
 * Phase 1 ships the dashboard (with AppShell layout) and the auth page
 * (login + register, outside the AppShell so it has its own full-screen layout).
 * Phase 2 will add nested routes under `/orders`, `/scheduling`, etc.
 * — keeping the AppShell layout consistent.
 */
import { createBrowserRouter } from 'react-router-dom';

import { AppShell } from '@/components/layout/AppShell';
import { AuthPage } from '@/features/auth/components/AuthPage';
import { DashboardPage } from '@/features/dashboard/components/DashboardPage';
import { OrdersPage } from '@/features/orders/components/OrdersPage';

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <DashboardPage /> },
      // Phase 2 stubs go here:
      { path: 'orders', element: <OrdersPage /> },
      // { path: 'scheduling', element: <SchedulingPage /> },
    ],
  },
  // Auth pages live outside the AppShell so they have their own full-screen layout.
  { path: '/login', element: <AuthPage /> },
  { path: '/register', element: <AuthPage /> },
]);
