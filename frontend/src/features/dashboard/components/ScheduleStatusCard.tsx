/**
 * Scheduler lifecycle status card.
 *
 * One-glance view of `schedule:status` — what state the worker is in,
 * which task is most recently in flight, and (on failure) the exception
 * string that came out of the worker.
 *
 * **Derived status logic (UX vs. raw API)**:
 * The raw ``schedule:status.state`` reflects "is a Celery task body
 * currently running" — but the per-compound architecture (PR-14)
 * flips state back to ``idle`` between every compound, even when the
 * queue still has work. A naive badge of "idle" + 400-deep queue feels
 * contradictory to operators. We derive a richer display:
 *
 *   - state=running                                    → Running (blue)
 *   - state=failed                                     → Failed (red)
 *   - state=idle, queue=0                              → Idle (green)
 *   - state=idle, queue>0, finished_at < STALL_S ago   → Working (blue)
 *                                                        (between-task gap)
 *   - state=idle, queue>0, finished_at >= STALL_S ago  → Stalled (orange)
 *                                                        (no task progress
 *                                                         in STALL_S — likely
 *                                                         worker crashed
 *                                                         mid-cycle)
 *
 * STALL_S = 30s is well above a normal between-task gap (~100–500ms)
 * and well below the operator-visible MTTR for restarting Celery.
 */
import { Activity, AlertTriangle, CheckCircle2, Loader2 } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

import type { ScheduleStatusResponse } from '../types';

interface ScheduleStatusCardProps {
  data: ScheduleStatusResponse | undefined;
  isLoading: boolean;
  isError: boolean;
  /** Current pending-ops queue depth (from /schedule/pending-ops). */
  queueDepth?: number;
}

interface Display {
  label: string;
  variant: 'success' | 'info' | 'warning' | 'destructive';
  Icon: typeof CheckCircle2;
  hint?: string;
}

const STALL_THRESHOLD_SECONDS = 30;

/**
 * Compute the user-facing badge from raw scheduler state + queue depth.
 *
 * Pure function — easy to unit-test independently of the React component.
 */
export function deriveScheduleDisplay(
  data: ScheduleStatusResponse,
  queueDepth: number | undefined,
  now: number = Date.now(),
): Display {
  if (data.state === 'failed') {
    return { label: 'Failed', variant: 'destructive', Icon: AlertTriangle };
  }
  if (data.state === 'running') {
    return { label: 'Running', variant: 'info', Icon: Loader2 };
  }
  // state === 'idle' below.
  if (!queueDepth || queueDepth <= 0) {
    return { label: 'Idle', variant: 'success', Icon: CheckCircle2 };
  }
  // idle + queue>0 → between-task gap or stalled.
  const finishedMs = data.finished_at ? Date.parse(data.finished_at) : NaN;
  const secondsSinceFinish = Number.isFinite(finishedMs) ? (now - finishedMs) / 1000 : Infinity;
  if (secondsSinceFinish < STALL_THRESHOLD_SECONDS) {
    return {
      label: 'Working',
      variant: 'info',
      Icon: Loader2,
      hint: 'Between compounds — next task firing soon',
    };
  }
  return {
    label: 'Stalled',
    variant: 'warning',
    Icon: AlertTriangle,
    hint: `Queue has work but no task has run in ${Math.round(secondsSinceFinish)}s — worker may be stuck`,
  };
}

export function ScheduleStatusCard({
  data,
  isLoading,
  isError,
  queueDepth,
}: ScheduleStatusCardProps): JSX.Element {
  if (isLoading) {
    return (
      <Card>
        <CardContent className="p-5">
          <Skeleton data-testid="schedule-status-skeleton" className="h-20 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (isError || !data) {
    return (
      <Card className="border-destructive/40">
        <CardContent className="flex items-start gap-3 p-5">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
          <p className="text-sm">Failed to load scheduler status.</p>
        </CardContent>
      </Card>
    );
  }

  const display = deriveScheduleDisplay(data, queueDepth);
  const { Icon } = display;
  const spin = display.label === 'Running' || display.label === 'Working';

  return (
    <Card>
      <CardContent className="space-y-3 p-5">
        <div className="flex flex-wrap items-center gap-3">
          <Activity className="h-4 w-4 text-muted-foreground" aria-hidden />
          <h3 className="text-sm font-semibold">Scheduler</h3>
          <Badge variant={display.variant}>
            <Icon className={`h-3 w-3 ${spin ? 'animate-spin' : ''}`} aria-hidden /> {display.label}
          </Badge>
          {queueDepth !== undefined && queueDepth > 0 ? (
            <Badge variant="outline" className="font-mono tabular-nums">
              queue: {queueDepth}
            </Badge>
          ) : null}
        </div>

        {display.hint ? (
          <p
            className={`text-xs ${
              display.variant === 'warning'
                ? 'text-amber-700 dark:text-amber-300'
                : 'text-muted-foreground'
            }`}
          >
            {display.hint}
          </p>
        ) : null}

        {data.message ? (
          <p className="text-xs text-muted-foreground">{data.message}</p>
        ) : (
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
            {data.task_id ? (
              <>
                <dt className="text-muted-foreground">Task ID</dt>
                <dd className="font-mono tabular-nums">{data.task_id}</dd>
              </>
            ) : null}
            {data.started_at ? (
              <>
                <dt className="text-muted-foreground">Started</dt>
                <dd className="tabular-nums">{formatTimestamp(data.started_at)}</dd>
              </>
            ) : null}
            {data.finished_at ? (
              <>
                <dt className="text-muted-foreground">Finished</dt>
                <dd className="tabular-nums">{formatTimestamp(data.finished_at)}</dd>
              </>
            ) : null}
          </dl>
        )}

        {data.error ? (
          <p className="rounded-md bg-destructive/10 px-2 py-1.5 text-xs text-destructive">
            {data.error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return iso;
  }
}
