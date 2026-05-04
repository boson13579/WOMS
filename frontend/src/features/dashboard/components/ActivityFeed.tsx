/**
 * Recent activity feed — Tier 4b.
 *
 * Mirrors the "audit log" list component on Vuetify dashboards but rendered
 * lighter. Each row: kind icon + message + actor + relative time.
 */
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Pencil, Plus, Settings, Trash2 } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

import type { ActivityItem, ActivityKind } from '../types';

const KIND_CONFIG: Record<ActivityKind, { icon: LucideIcon; bg: string; text: string }> = {
  create: {
    icon: Plus,
    bg: 'bg-emerald-50 dark:bg-emerald-500/15',
    text: 'text-emerald-600 dark:text-emerald-400',
  },
  update: {
    icon: Pencil,
    bg: 'bg-sky-50 dark:bg-sky-500/15',
    text: 'text-sky-600 dark:text-sky-400',
  },
  delete: {
    icon: Trash2,
    bg: 'bg-rose-50 dark:bg-rose-500/15',
    text: 'text-rose-600 dark:text-rose-400',
  },
  system: { icon: Settings, bg: 'bg-muted', text: 'text-muted-foreground' },
};

interface ActivityFeedProps {
  items: readonly ActivityItem[];
}

export function ActivityFeed({ items }: ActivityFeedProps): JSX.Element {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle>Recent activity</CardTitle>
        <CardDescription>audit log · last 8 events</CardDescription>
      </CardHeader>
      <CardContent>
        <ol className="space-y-3">
          {items.map((item) => {
            const cfg = KIND_CONFIG[item.kind];
            const Icon = cfg.icon;
            const relative = formatDistanceToNow(parseISO(item.timestamp), { addSuffix: true });
            return (
              <li key={item.id} className="flex items-start gap-3">
                <span
                  className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${cfg.bg}`}
                >
                  <Icon className={`h-3.5 w-3.5 ${cfg.text}`} />
                </span>
                <div className="min-w-0 flex-1 leading-tight">
                  <p className="truncate text-sm">{item.message}</p>
                  <p className="text-[11px] text-muted-foreground">
                    <span className="font-medium text-foreground/80">{item.actor}</span>
                    <span className="mx-1.5 text-border">·</span>
                    {relative}
                  </p>
                </div>
              </li>
            );
          })}
        </ol>
      </CardContent>
    </Card>
  );
}
