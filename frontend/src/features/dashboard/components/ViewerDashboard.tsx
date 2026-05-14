/**
 * Dashboard view for ``viewer`` role users.
 *
 * New registrations default to ``viewer`` (per `feedback_new_user_default`
 * — root must promote before they see scheduling data). The viewer
 * dashboard is intentionally sparse: a welcome card, a profile snapshot,
 * a static "please contact your administrator" notice, and the Service
 * Health row (the only operational widget viewers can see).
 *
 * The page keeps the same chrome (Header) as the full dashboard so
 * navigation / theme controls are consistent.
 */
import { Info, ShieldCheck } from 'lucide-react';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { useCurrentRole, useCurrentUser } from '@/lib/auth';

import { useSystemHealth } from '../api/useSystemHealth';

import { ServiceHealthGrid } from './ServiceHealthGrid';

export function ViewerDashboard(): JSX.Element {
  const user = useCurrentUser();
  const role = useCurrentRole();
  const systemHealth = useSystemHealth();

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-muted-foreground" aria-hidden />
            Welcome{user ? `, ${user.username}` : ''}
          </CardTitle>
          <CardDescription>
            Your current role is <span className="font-medium">{role ?? 'viewer'}</span>. Most
            dashboard widgets are gated to order-manager-and-above; please contact your
            administrator if you need elevated access.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="flex items-start gap-2 rounded-md bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            <span>
              You can still verify whether the API and supporting services are reachable below.
            </span>
          </p>
        </CardContent>
      </Card>

      <section aria-label="Service health">
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Services
        </h2>
        <ServiceHealthGrid
          data={systemHealth.data}
          isLoading={systemHealth.isLoading}
          isError={systemHealth.isError}
        />
      </section>
    </div>
  );
}
