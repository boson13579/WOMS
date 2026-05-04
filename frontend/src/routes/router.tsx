/**
 * Route definitions for the Smart Order SPA.
 *
 * Phase 1: only the auth route exists. Phase 2 will add:
 *   /dashboard  — order overview
 *   /orders     — order CRUD
 *   /schedule   — calendar view
 *   /account    — account management
 *
 * Uses react-router-dom v6 `createBrowserRouter` + `RouterProvider`.
 * Protected routes will be gated by the Zustand authStore token.
 */
import { createBrowserRouter } from 'react-router-dom';

import { AuthPage } from '@/features/auth/components/AuthPage';

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AuthPage />,
  },
  {
    path: '/login',
    element: <AuthPage />,
  },
  {
    path: '/register',
    element: <AuthPage />,
  },
  // Phase 2 routes — uncomment and add lazy imports when implemented:
  // { path: '/dashboard', element: <DashboardPage /> },
  // { path: '/orders',    element: <OrdersPage /> },
  // { path: '/schedule',  element: <SchedulePage /> },
  // { path: '/account',   element: <AccountPage /> },
]);
