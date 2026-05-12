/**
 * Scheduler lifecycle status card.
 *
 * One-glance view of `schedule:status` — what state the worker is in,
 * which task is most recently in flight, and (on failure) the exception
 * string that came out of the worker. Pure presentation; the calling
 * page owns the React Query lifecycle and threads loading / error /
 * data through props.
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
}

// ``running`` uses ``info`` (blue) so it reads as "in progress" rather
// than success or warning. ``failed`` is ``destructive`` (red) to match
// the rest of the app's failure semantics.
const STATE_CFG = {
  idle: { label: 'Idle', variant: 'success' as const, Icon: CheckCircle2 },
  running: { label: 'Running', variant: 'info' as const, Icon: Loader2 },
  failed: { label: 'Failed', variant: 'destructive' as const, Icon: AlertTriangle },
};

export function ScheduleStatusCard({
  data,
  isLoading,
  isError,
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

  const cfg = STATE_CFG[data.state];
  const { Icon } = cfg;

  return (
    <Card>
      <CardContent className="space-y-3 p-5">
        <div className="flex items-center gap-3">
          <Activity className="h-4 w-4 text-muted-foreground" aria-hidden />
          <h3 className="text-sm font-semibold">Scheduler</h3>
          <Badge variant={cfg.variant}>
            <Icon className="h-3 w-3" aria-hidden /> {cfg.label}
          </Badge>
        </div>

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
