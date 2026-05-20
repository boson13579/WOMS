/**
 * SessionBoundary — pathless route element that wires the global 401
 * handler exactly once at app boot.
 *
 * Lives INSIDE the router tree (as the ``element`` of a pathless
 * top-level route) so ``useNavigate`` is bound to the router context;
 * placing the boundary outside ``<RouterProvider>`` would throw the
 * familiar "useNavigate() may be used only in the context of a
 * <Router> component" error. The pathless wrapper guarantees every
 * subsequent route renders inside this effect's scope.
 *
 * The handler runs ``queryClient.clear() + logout() + navigate(/login?
 * next=…)`` synchronously enough that a 401 anywhere in the app is
 * indistinguishable from a deliberate logout, from the user's
 * perspective.
 */
import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { Outlet, useNavigate } from 'react-router-dom';

import { installUnauthorizedHandler } from '@/features/auth/stores/authStore';

export function SessionBoundary(): JSX.Element {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  useEffect(() => {
    installUnauthorizedHandler(queryClient, (path, opts) => {
      navigate(path, opts);
    });
  }, [queryClient, navigate]);

  return <Outlet />;
}
