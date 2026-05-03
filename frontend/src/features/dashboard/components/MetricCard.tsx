/**
 * Resource utilization card with embedded sparkline (Tier 2).
 *
 * Top row: metric name + current value (large) + trend pill (vs avg).
 * Bottom: 60-point area sparkline rendered with Recharts. The accent color
 * comes from a Tailwind palette pre-mapped to a `from-*` / `text-*` class so
 * the four cards visually differ but stay coherent.
 */
import { TrendingDown, TrendingUp } from 'lucide-react';
import { Area, AreaChart, ResponsiveContainer, Tooltip } from 'recharts';

import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';

import type { ResourceMetric } from '../types';

const ACCENT_MAP: Record<ResourceMetric['accent'], { stroke: string }> = {
  emerald: { stroke: '#10b981' },
  sky: { stroke: '#0ea5e9' },
  violet: { stroke: '#8b5cf6' },
  amber: { stroke: '#f59e0b' },
};

export function MetricCard({ metric }: { metric: ResourceMetric }): JSX.Element {
  const accent = ACCENT_MAP[metric.accent];
  const delta = metric.currentNumeric - metric.averageNumeric;
  const trending = delta >= 0 ? 'up' : 'down';
  const TrendIcon = trending === 'up' ? TrendingUp : TrendingDown;
  const gradId = `metric-grad-${metric.id}`;

  return (
    <Card className="overflow-hidden hover:shadow-md">
      <CardContent className="p-5 pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
              {metric.name}
            </p>
            <p className="mt-1 text-2xl font-semibold tabular-nums tracking-tight">
              {metric.current}
            </p>
          </div>
          <span
            className={cn(
              'inline-flex items-center gap-0.5 rounded-full px-2 py-0.5 text-[11px] font-medium',
              trending === 'up'
                ? 'bg-rose-50 text-rose-700 dark:bg-rose-500/15 dark:text-rose-300'
                : 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300',
            )}
          >
            <TrendIcon className="h-3 w-3" />
            {Math.abs(delta).toFixed(0)}
            <span className="text-muted-foreground/80">%</span>
          </span>
        </div>
        <p className="mt-1 text-[11px] text-muted-foreground">
          last 60 min · {metric.averageLabel}
        </p>
      </CardContent>

      <div className="h-16 px-2 pb-1">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={[...metric.series]} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={accent.stroke} stopOpacity={0.35} />
                <stop offset="100%" stopColor={accent.stroke} stopOpacity={0} />
              </linearGradient>
            </defs>
            <Tooltip
              cursor={{ stroke: accent.stroke, strokeWidth: 1, strokeDasharray: '2 2' }}
              contentStyle={{
                background: 'hsl(var(--popover))',
                color: 'hsl(var(--popover-foreground))',
                border: '1px solid hsl(var(--border))',
                borderRadius: 6,
                fontSize: 11,
                padding: '4px 8px',
              }}
              labelFormatter={() => ''}
              formatter={(value: number) => [`${value}`, metric.name]}
            />
            <Area
              type="monotone"
              dataKey="v"
              stroke={accent.stroke}
              strokeWidth={1.5}
              fill={`url(#${gradId})`}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
