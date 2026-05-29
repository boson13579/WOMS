/**
 * Server-confirmed identity gate.
 *
 * Decisions:
 *   1. If no session is persisted, redirect to ``/login`` synchronously
 *      so an unauthed visit doesn't briefly flash protected content.
 *   2. Otherwise mount ``useMe()`` to confirm the cookie is still
 *      accepted server-side. While the query is pending, render a
 *      minimal splash (``role=status``) — the persisted hint is not
 *      good enough on its own (cookie may have been revoked or the
 *      account deactivated).
 *   3. On 401 the global handler installed by ``SessionBoundary`` has
 *      already cleared the cache + redirected; the boundary just
 *      renders nothing this render to avoid a flash of the Outlet.
 *   4. On non-401 errors (sustained 5xx, network blip past React
 *      Query's retry budget) render a Retry / Sign out card so the
 *      user is never stuck on a forever-spinning splash.
 */
import { Navigate, Outlet } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import { useAuthStore } from '@/features/auth/stores/authStore';
import { ApiError } from '@/lib/apiFetch';
import { useMe } from '@/lib/auth';

export function ProtectedRoute(): JSX.Element {
  // All hooks must run unconditionally on every render. ``useMe`` is
  // gated internally by ``enabled`` so it doesn't fire when the user is
  // null (the Navigate below short-circuits before we ever read it).
  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);
  const query = useMe({ enabled: user !== null });

  // Synchronous bail — no flash of protected content for unauthed users.
  // The expiresAt hint is intentionally NOT consulted here: trust the
  // server via ``useMe()`` once a user is persisted.
  if (!user) {
    return <Navigate to="/login" replace />;
  }

  if (query.isPending) {
    return (
      <div
        role="status"
        aria-live="polite"
        className="flex min-h-screen items-center justify-center bg-background"
      >
        <span className="text-sm text-muted-foreground">Verifying session…</span>
      </div>
    );
  }

  if (query.isError) {
    const status = query.error instanceof ApiError ? query.error.status : null;
    // 401 is handled globally by the unauthorized handler (clears cache,
    // navigates to /login). Render nothing this tick to avoid flashing
    // the outlet before navigation completes.
    if (status === 401) {
      // Render an empty div this tick — the global handler has already
      // scheduled a navigate; we just avoid flashing the Outlet.
      return <div aria-hidden="true" />;
    }
    return (
      <div className="flex min-h-screen items-center justify-center bg-background p-6">
        <div className="w-full max-w-sm rounded-md border border-border bg-card p-6 shadow-sm">
          <h2 className="text-base font-semibold text-foreground">Couldn&apos;t verify session</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            We couldn&apos;t reach the server to confirm your session is still valid. Check your
            connection and retry, or sign out and log in again.
          </p>
          <div className="mt-4 flex items-center gap-2">
            <Button
              type="button"
              size="sm"
              onClick={() => {
                query.refetch().catch(() => {});
              }}
            >
              Retry
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                logout().catch(() => {});
              }}
            >
              Sign out
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return <Outlet />;
}
