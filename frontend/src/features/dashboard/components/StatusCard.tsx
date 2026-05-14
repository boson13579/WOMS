/**
 * Service-health status card (Tier 1).
 *
 * Layout: status dot + service name on top row, summary subtitle, then 2–3
 * label/value pairs in a tight grid. Mirrors the Vuetify "list-card" look
 * but rendered with shadcn Card + Tailwind.
 */
import { CheckCircle2, AlertTriangle, XCircle } from 'lucide-react';

import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';

import type { ServiceHealthEntry } from '../types';

const STATUS_CONFIG = {
  healthy: {
    icon: CheckCircle2,
    label: 'Healthy',
    pillClass: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300',
    dotClass: 'bg-emerald-500',
  },
  warning: {
    icon: AlertTriangle,
    label: 'Warning',
    pillClass: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
    dotClass: 'bg-amber-500',
  },
  error: {
    icon: XCircle,
    label: 'Error',
    pillClass: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300',
    dotClass: 'bg-red-500',
  },
} as const;

export function StatusCard({ service }: { service: ServiceHealthEntry }): JSX.Element {
  const cfg = STATUS_CONFIG[service.status];
  const StatusIcon = cfg.icon;

  return (
    <Card className="hover:shadow-md">
      <CardContent className="p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  'inline-block h-2 w-2 shrink-0 rounded-full',
                  cfg.dotClass,
                  service.status === 'healthy' && 'animate-pulse',
                )}
                aria-hidden
              />
              <h3 className="truncate text-sm font-semibold">{service.name}</h3>
            </div>
            <p className="mt-0.5 truncate text-xs text-muted-foreground">{service.summary}</p>
          </div>
          <span
            className={cn(
              'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium',
              cfg.pillClass,
            )}
          >
            <StatusIcon className="h-3 w-3" />
            {cfg.label}
          </span>
        </div>

        <dl className="mt-4 grid grid-cols-3 gap-2">
          {service.details.map((detail) => (
            <div key={detail.label} className="space-y-0.5">
              <dt className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                {detail.label}
              </dt>
              <dd className="text-sm font-medium tabular-nums">{detail.value}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}
