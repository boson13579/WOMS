/**
 * Orders snapshot card — one tile per OrderStatus we surface on the dashboard.
 *
 * The four shown statuses (pending / scheduled / in_production / completed)
 * mirror the live row count a user sees on the Orders page. ``cancelled``
 * is intentionally absent: a cancelled order is a soft-deleted row and
 * the Orders page filters those out by default; surfacing the count here
 * would be misleading.
 */
import { AlertTriangle, CheckCircle2, Clock, Factory, ListChecks } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

import type { OrdersSnapshotCounts, OrdersSnapshotStatus } from '../types';

interface Tile {
  key: OrdersSnapshotStatus;
  label: string;
  Icon: LucideIcon;
  iconBg: string;
  iconText: string;
  /** Hover hint clarifying what the count actually means. Especially
   *  important for ``pending``, which is the most-asked "what does this
   *  number actually count?" tile because of its overlap with the
   *  scheduler-queue's Pending Operations widget — they look the same
   *  but measure different things. */
  hint: string;
}

const TILES: readonly Tile[] = [
  {
    key: 'pending',
    label: 'Pending',
    Icon: Clock,
    iconBg: 'bg-amber-50 dark:bg-amber-500/15',
    iconText: 'text-amber-600 dark:text-amber-400',
    hint: 'Orders the scheduler has not finished placing yet (newly added, or modified in a way that needs re-scheduling). Different from Pending Operations: that card counts every queued operation including pin / unpin / cancel which do not flip an order back to pending.',
  },
  {
    key: 'scheduled',
    label: 'Scheduled',
    Icon: ListChecks,
    iconBg: 'bg-sky-50 dark:bg-sky-500/15',
    iconText: 'text-sky-600 dark:text-sky-400',
    hint: 'Orders with a confirmed production date slot. They can still receive pin / unpin / metadata edits while in this state.',
  },
  {
    key: 'in_production',
    label: 'In production',
    Icon: Factory,
    iconBg: 'bg-violet-50 dark:bg-violet-500/15',
    iconText: 'text-violet-600 dark:text-violet-400',
    hint: 'Orders whose production day has arrived (flipped daily by advance_day at 00:00 UTC).',
  },
  {
    key: 'completed',
    label: 'Completed',
    Icon: CheckCircle2,
    iconBg: 'bg-emerald-50 dark:bg-emerald-500/15',
    iconText: 'text-emerald-600 dark:text-emerald-400',
    hint: 'Orders whose production day has passed.',
  },
];

interface OrdersSnapshotCardProps {
  data: OrdersSnapshotCounts | undefined;
  isLoading: boolean;
  isError: boolean;
}

export function OrdersSnapshotCard({
  data,
  isLoading,
  isError,
}: OrdersSnapshotCardProps): JSX.Element {
  if (isLoading) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Orders</CardTitle>
          <CardDescription>by DB order status — pending = not yet scheduled</CardDescription>
        </CardHeader>
        <CardContent>
          <Skeleton data-testid="orders-snapshot-skeleton" className="h-40 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (isError || !data) {
    return (
      <Card className="border-destructive/40">
        <CardContent className="flex items-start gap-3 p-5">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
          <p className="text-sm">Failed to load orders snapshot.</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle>Orders</CardTitle>
        <CardDescription>by DB order status — pending = not yet scheduled</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-3">
          {TILES.map((tile) => (
            <div
              key={tile.key}
              title={tile.hint}
              className="flex items-center gap-3 rounded-md border border-border/60 bg-background p-3"
            >
              <span
                className={`flex h-9 w-9 items-center justify-center rounded-md ${tile.iconBg}`}
              >
                <tile.Icon className={`h-4 w-4 ${tile.iconText}`} />
              </span>
              <div>
                <p className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  {tile.label}
                </p>
                <p className="text-xl font-semibold tabular-nums tracking-tight">
                  {data[tile.key].toLocaleString()}
                </p>
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
