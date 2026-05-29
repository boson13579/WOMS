/**
 * Inverse-guard sibling of ``ProtectedRoute``.
 *
 * Wraps ``/login`` and ``/register`` so an already-authed user who
 * navigates back (e.g. via the browser history) gets routed to their
 * dashboard instead of seeing the login form. Honours a ``?next=``
 * query param so a global 401 → login → success cycle lands the user
 * where they were.
 *
 * Decisions are synchronous on the persisted store state. ``useMe()``
 * is intentionally NOT consulted here — fresh logins must redirect
 * immediately without waiting for the round-trip. If the cookie has
 * actually expired server-side, the destination's ``ProtectedRoute`` →
 * ``useMe()`` will surface the 401 and bounce back; that one-frame
 * flash is the documented trade-off.
 */
import { type ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { useAuthStore } from '@/features/auth/stores/authStore';
import { safeNextPath } from '@/lib/safeNextPath';

interface AuthOnlyRouteProps {
  children: ReactNode;
}

export function AuthOnlyRoute({ children }: Readonly<AuthOnlyRouteProps>): JSX.Element {
  const user = useAuthStore((state) => state.user);
  const expiresAt = useAuthStore((state) => state.expiresAt);
  const location = useLocation();

  const isAuthed = Boolean(user && expiresAt && expiresAt > Date.now());
  if (isAuthed) {
    // ``safeNextPath`` rejects open-redirect-shaped inputs (absolute URLs,
    // protocol-relative, missing leading slash) and collapses them to
    // ``/`` — see ``frontend/src/lib/safeNextPath.ts``.
    const params = new URLSearchParams(location.search);
    const next = safeNextPath(params.get('next'));
    return <Navigate to={next} replace />;
  }

  // children is ReactNode (potentially multiple elements); render-as-is.
  // eslint-disable-next-line react/jsx-no-useless-fragment
  return <>{children}</>;
}
