/**
 * UseResourceCards — three-card row for the USE block.
 *
 * Each card reads its own slice of the ``/system/resources`` response
 * and degrades independently when its section is ``null`` (probe
 * failed).
 *
 * Card layout (post-Phase-2 observability revamp):
 *   1. DB connections — aggregate across all backend replicas, with the
 *      per-replica breakdown rendered in the caption so an uneven
 *      nginx round-robin is visible at a glance.
 *   2. Redis — saturation bar against ``max_memory_bytes`` (the
 *      configured cap). When unset (=0, the docker default) we drop the
 *      bar entirely and surface raw MB + ``evicted_keys`` instead;
 *      ``used_memory_peak`` was the previous denominator but that's a
 *      high-water mark, not a budget, so it made the bar permanently
 *      red on a quiet system.
 *   3. Live connections — total active WebSocket sessions across all
 *      backend replicas (was: Workers / Celery). Workers were single-
 *      replica and not a bottleneck; this card shows "how many
 *      dashboards are currently watching" which is operationally
 *      meaningful and visually responsive during a demo.
 */
import { AlertTriangle } from 'lucide-react';

import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

import type { UseResources } from '../types';

import { UseResourceCard } from './UseResourceCard';

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

/**
 * Compact a pod id for caption rendering. Both docker (hex container id
 * prefix, e.g. ``7e004a4e76de``) and k8s (pod name, e.g.
 * ``backend-7d8c9b4f5-abc12``) get usefully distinguished by taking the
 * trailing 6 chars — the random suffix in k8s, the hex tail in docker.
 * Prefix-based slicing would collapse every k8s replica to ``backend-``
 * which defeats the per-replica breakdown.
 */
function shortPodId(id: string): string {
  return id.length <= 6 ? id : id.slice(-6);
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
  // Layout:
  //   "16 / 50"  [detail: "(32.0% used)"]
  //   bar
  //   backend-1: 9/25
  //   backend-2: 7/25
  const db = data.db_pool;
  const dbValue = db ? `${db.checked_out} / ${db.size + db.max_overflow}` : null;
  const dbDetail = db ? `(${db.utilization_pct.toFixed(1)}% used)` : undefined;
  const dbRatio = db ? db.utilization_pct / 100 : null;
  const dbCaption: string | undefined =
    db && db.replicas.length > 1
      ? db.replicas
          .map((r) => `${shortPodId(r.pod_id)}: ${r.checked_out} / ${r.size + r.max_overflow}`)
          .join(' · ')
      : undefined;

  // ---- Redis ------------------------------------------------------------
  // Two layout modes depending on whether a maxmemory cap is configured:
  // - Capped (AWS ElastiCache): one-line caption + saturation bar
  // - Uncapped (docker default): caption split into two lines so the
  //   "no cap" status is visually distinct from the client count
  const { redis } = data;
  const redisValue = redis ? formatBytes(redis.used_memory_bytes) : null;
  const redisRatio =
    redis && redis.max_memory_bytes > 0 ? redis.used_memory_bytes / redis.max_memory_bytes : null;
  const clientsLine = redis
    ? `${redis.connected_clients} client${redis.connected_clients === 1 ? '' : 's'}${
        redis.evicted_keys > 0 ? ` · ${redis.evicted_keys} evicted` : ''
      }`
    : undefined;
  let redisCaption: string | string[] | undefined;
  if (!redis) {
    redisCaption = undefined;
  } else if (redis.max_memory_bytes > 0) {
    redisCaption = `cap ${formatBytes(redis.max_memory_bytes)} · ${clientsLine ?? ''}`;
  } else {
    redisCaption = ['no cap', clientsLine ?? ''];
  }

  // ---- Live WebSocket connections (replaces Workers card) ----------------
  // Layout: just the number + per-pod breakdown on separate lines, no
  // "session"/"sessions" suffix (number on its own is unambiguous).
  const ws = data.ws_connections;
  const wsValue = ws ? `${ws.total}` : null;
  const wsCaption: string[] | undefined =
    ws && ws.replicas.length > 1
      ? ws.replicas.map((r) => `${shortPodId(r.pod_id)}: ${r.count}`)
      : undefined;

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
      <UseResourceCard
        label="DB connections"
        value={dbValue}
        detail={dbDetail}
        ratio={dbRatio}
        caption={dbCaption}
        unreachableMessage="Pool stats unavailable."
      />
      <UseResourceCard
        label="Redis memory"
        value={redisValue}
        ratio={redisRatio}
        caption={redisCaption}
        // No ``maxmemory`` configured (local docker default) → no
        // meaningful denominator, hide the bar slot entirely instead
        // of showing a dashed "no signal" placeholder that the eye
        // mistakes for "still loading".
        hideBar={!redis || redis.max_memory_bytes === 0}
        unreachableMessage="Redis unreachable."
      />
      <UseResourceCard
        label="Live connections"
        value={wsValue}
        caption={wsCaption}
        // No natural saturation denominator — see ``hideBar`` docs.
        hideBar
        unreachableMessage="WebSocket stats unavailable."
      />
    </div>
  );
}
