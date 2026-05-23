/**
 * UseResourceCards — three-card row for the USE block.
 *
 * Each card reads its own slice of the ``/system/resources`` response
 * and degrades independently when its section is ``null`` (probe
 * failed). The Workers card threads a per-worker drilldown through
 * the generic ``UseResourceCard``'s ``expandable`` slot.
 */
import { AlertTriangle } from 'lucide-react';

import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

import type { UseResources } from '../types';

import { UseResourceCard } from './UseResourceCard';
import { WorkersDrilldown } from './WorkersDrilldown';

interface UseResourceCardsProps {
  data: UseResources | undefined;
  isLoading: boolean;
  isError: boolean;
}

function formatBytes(n: number): string {
  if (n === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log10(n) / 3));
  const v = n / 10 ** (i * 3);
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

export function UseResourceCards({ data, isLoading, isError }: UseResourceCardsProps): JSX.Element {
  if (isLoading && !data) {
    return (
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        {Array.from({ length: 3 }, (_, i) => (
          // eslint-disable-next-line react/no-array-index-key
          <Skeleton key={i} data-testid="use-resource-skeleton" className="h-40 w-full" />
        ))}
      </div>
    );
  }

  if (isError || !data) {
    return (
      <Card className="border-destructive/40">
        <CardContent className="flex items-start gap-3 p-5">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
          <p className="text-sm">Failed to load resources.</p>
        </CardContent>
      </Card>
    );
  }

  // ---- DB pool ----------------------------------------------------------
  const db = data.db_pool;
  const dbValue = db ? `${db.checked_out} / ${db.size + db.max_overflow}` : null;
  const dbRatio = db ? db.utilization_pct / 100 : null;
  const dbCaption = db ? `${db.utilization_pct.toFixed(1)} % used` : undefined;

  // ---- Redis ------------------------------------------------------------
  const { redis } = data;
  const redisValue = redis ? formatBytes(redis.used_memory_bytes) : null;
  // We don't get max_memory from /resources; use peak as a soft denominator
  // to give the bar a visual anchor when the peak is non-trivial.
  const redisRatio =
    redis && redis.used_memory_peak_bytes > 0
      ? redis.used_memory_bytes / redis.used_memory_peak_bytes
      : null;
  const redisCaption = redis
    ? `peak ${formatBytes(redis.used_memory_peak_bytes)} · ${redis.connected_clients} client${redis.connected_clients === 1 ? '' : 's'}`
    : undefined;

  // ---- Celery / workers -------------------------------------------------
  const { celery } = data;
  const celeryValue = celery
    ? `${celery.registered_workers} up · ${celery.active_tasks} active`
    : null;
  const celeryDetail = celery ? `${celery.queue_depth} pending` : undefined;
  // No natural denominator for "workers utilization"; saturation is
  // queue_depth-vs-workers, capped at 1.0 for the bar visual.
  const celeryRatio =
    celery && celery.registered_workers > 0
      ? Math.min(1, celery.queue_depth / (celery.registered_workers * 5))
      : null;
  const celeryCaption = celery?.truncated
    ? `showing 50 of ${celery.registered_workers} workers`
    : undefined;

  // Per the plan: don't render the expand button when ``workers.length <= 1``
  // — a single worker has nothing to drill into.
  const workersExpandable =
    celery && celery.workers.length > 1 ? <WorkersDrilldown workers={celery.workers} /> : undefined;

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
      <UseResourceCard
        label="DB connections"
        value={dbValue}
        ratio={dbRatio}
        caption={dbCaption}
        unreachableMessage="Pool stats unavailable."
      />
      <UseResourceCard
        label="Redis memory"
        value={redisValue}
        ratio={redisRatio}
        caption={redisCaption}
        unreachableMessage="Redis unreachable."
      />
      <UseResourceCard
        label="Workers"
        value={celeryValue}
        detail={celeryDetail}
        ratio={celeryRatio}
        caption={celeryCaption}
        expandable={workersExpandable}
        unreachableMessage="Celery unreachable."
      />
    </div>
  );
}
