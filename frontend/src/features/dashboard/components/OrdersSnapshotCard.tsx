/**
 * Orders snapshot — Tier 4a.
 *
 * Four small numeric tiles in one card (today / pending / scheduled / done).
 * Phase 1 shows mock counts; Phase 2 will pull from `/api/v1/orders/stats`.
 */
import { CheckCircle2, Clock, ListChecks, PackagePlus } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

import type { OrdersSnapshot } from '../types';

interface Tile {
  key: keyof OrdersSnapshot;
  label: string;
  icon: LucideIcon;
  iconBg: string;
  iconText: string;
}

interface OrdersSnapshotCardProps {
  snapshot: OrdersSnapshot;
}

const TILES: readonly Tile[] = [
  {
    key: 'newToday',
    label: 'New today',
    icon: PackagePlus,
    iconBg: 'bg-emerald-50 dark:bg-emerald-500/15',
    iconText: 'text-emerald-600 dark:text-emerald-400',
  },
  {
    key: 'pendingSchedule',
    label: 'Pending',
    icon: Clock,
    iconBg: 'bg-amber-50 dark:bg-amber-500/15',
    iconText: 'text-amber-600 dark:text-amber-400',
  },
  {
    key: 'scheduled',
    label: 'Scheduled',
    icon: ListChecks,
    iconBg: 'bg-sky-50 dark:bg-sky-500/15',
    iconText: 'text-sky-600 dark:text-sky-400',
  },
  {
    key: 'completed',
    label: 'Completed',
    icon: CheckCircle2,
    iconBg: 'bg-violet-50 dark:bg-violet-500/15',
    iconText: 'text-violet-600 dark:text-violet-400',
  },
];

export function OrdersSnapshotCard({ snapshot }: OrdersSnapshotCardProps): JSX.Element {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle>Orders today</CardTitle>
        <CardDescription>snapshot · refreshes every 30 s</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-3">
          {TILES.map(({ key, label, icon: Icon, iconBg, iconText }) => (
            <div
              key={key}
              className="flex items-center gap-3 rounded-md border border-border/60 bg-background p-3"
            >
              <span className={`flex h-9 w-9 items-center justify-center rounded-md ${iconBg}`}>
                <Icon className={`h-4 w-4 ${iconText}`} />
              </span>
              <div>
                <p className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  {label}
                </p>
                <p className="text-xl font-semibold tabular-nums tracking-tight">{snapshot[key]}</p>
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
