/**
 * Latency percentile chart (p50 / p95 / p99) — Tier 3b.
 */
import { format, parseISO } from 'date-fns';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

import type { LatencySeriesPoint } from '../types';

interface LatencyChartProps {
  series: readonly LatencySeriesPoint[];
}

export function LatencyChart({ series }: LatencyChartProps): JSX.Element {
  const data = series.map((p) => ({ ...p, ts: format(parseISO(p.t), 'HH:mm') }));
  const lastP95 = data[data.length - 1]?.p95 ?? 0;

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between space-y-0 pb-2">
        <div>
          <CardTitle>Latency</CardTitle>
          <CardDescription>last 60 min · p50 / p95 / p99</CardDescription>
        </div>
        <div className="text-right">
          <p className="text-2xl font-semibold tabular-nums tracking-tight">{lastP95}</p>
          <p className="text-xs text-muted-foreground">ms (p95)</p>
        </div>
      </CardHeader>
      <CardContent>
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
              <XAxis
                dataKey="ts"
                tickLine={false}
                axisLine={false}
                stroke="hsl(var(--muted-foreground))"
                fontSize={11}
                interval={9}
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                stroke="hsl(var(--muted-foreground))"
                fontSize={11}
                unit=" ms"
              />
              <Tooltip
                contentStyle={{
                  background: 'hsl(var(--popover))',
                  color: 'hsl(var(--popover-foreground))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: 6,
                  fontSize: 12,
                }}
              />
              <Legend
                verticalAlign="top"
                height={28}
                iconType="line"
                iconSize={14}
                wrapperStyle={{ fontSize: 12 }}
              />
              <Line
                name="p50"
                type="monotone"
                dataKey="p50"
                stroke="#94a3b8"
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
              <Line
                name="p95"
                type="monotone"
                dataKey="p95"
                stroke="#0ea5e9"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
              <Line
                name="p99"
                type="monotone"
                dataKey="p99"
                stroke="#8b5cf6"
                strokeWidth={2}
                strokeDasharray="3 2"
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}
