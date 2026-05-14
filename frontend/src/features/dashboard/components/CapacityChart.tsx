/**
 * 30-day remaining-capacity prefix sum, rendered as an area chart.
 *
 * Reads {@link ScheduleCapacityResponse} as a prop (page owns React
 * Query). x-axis ticks every 5 days to keep the 30-day window readable;
 * y-axis is the cumulative-remaining count which is the same number the
 * scheduler uses for feasibility checks, so this chart literally shows
 * "how much wafer-capacity slack do we still have over the horizon".
 */
import { format, parseISO } from 'date-fns';
import { AlertTriangle, BarChart3 } from 'lucide-react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

import type { ScheduleCapacityResponse } from '../types';

interface CapacityChartProps {
  data: ScheduleCapacityResponse | undefined;
  isLoading: boolean;
  isError: boolean;
}

export function CapacityChart({ data, isLoading, isError }: CapacityChartProps): JSX.Element {
  if (isLoading) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle>Capacity (next 30 days)</CardTitle>
          <CardDescription>cumulative remaining wafer slots</CardDescription>
        </CardHeader>
        <CardContent>
          <Skeleton data-testid="capacity-chart-skeleton" className="h-64 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (isError || !data) {
    return (
      <Card className="border-destructive/40">
        <CardContent className="flex items-start gap-3 p-5">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
          <p className="text-sm">Failed to load capacity data.</p>
        </CardContent>
      </Card>
    );
  }

  const chartData = data.entries.map((e) => ({
    ts: format(parseISO(e.date), 'M/d'),
    remaining: e.cumulative_remaining,
  }));
  // ``.at(-1)`` returns ``T | undefined`` (vs. raw index access which
  // ts-narrows to never-undefined without ``noUncheckedIndexedAccess``).
  // We need the genuine optional so the empty-entries branch below isn't
  // dead-coded out by the type checker.
  const lastEntry = data.entries.at(-1);

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between space-y-0 pb-2">
        <div>
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="h-4 w-4 text-muted-foreground" aria-hidden />
            Capacity (next 30 days)
          </CardTitle>
          <CardDescription>
            cumulative remaining wafer slots · {data.daily_capacity.toLocaleString()}/day cap
          </CardDescription>
        </div>
        {lastEntry ? (
          <div className="text-right">
            <p className="text-2xl font-semibold tabular-nums tracking-tight">
              {lastEntry.cumulative_remaining.toLocaleString()}
            </p>
            <p className="text-xs text-muted-foreground">total slack</p>
          </div>
        ) : null}
      </CardHeader>
      <CardContent>
        <div className="h-64">
          {chartData.length === 0 ? (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              No capacity data yet.
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 6, right: 12, bottom: 0, left: -8 }}>
                <defs>
                  <linearGradient id="capacity-fill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
                <XAxis
                  dataKey="ts"
                  tickLine={false}
                  axisLine={false}
                  stroke="hsl(var(--muted-foreground))"
                  fontSize={11}
                  interval={4}
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  stroke="hsl(var(--muted-foreground))"
                  fontSize={11}
                  tickFormatter={(v: number) => v.toLocaleString()}
                  width={64}
                />
                <Tooltip
                  contentStyle={{
                    background: 'hsl(var(--popover))',
                    color: 'hsl(var(--popover-foreground))',
                    border: '1px solid hsl(var(--border))',
                    borderRadius: 6,
                    fontSize: 12,
                  }}
                  formatter={(value: number) => [value.toLocaleString(), 'remaining']}
                />
                <Area
                  type="monotone"
                  dataKey="remaining"
                  stroke="hsl(var(--primary))"
                  strokeWidth={2}
                  fill="url(#capacity-fill)"
                />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
