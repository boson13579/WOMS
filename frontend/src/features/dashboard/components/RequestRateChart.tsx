/**
 * Stacked area chart of request rate split by status class.
 *
 * Tier 3a — wide card. Uses CSS variables for axis colors so dark mode
 * inherits automatically (planned for Phase 2).
 */
import { format, parseISO } from 'date-fns';
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

import type { RequestSeriesPoint } from '../types';

const COLORS = {
  ok: '#10b981',
  clientErr: '#f59e0b',
  serverErr: '#ef4444',
};

interface RequestRateChartProps {
  series: readonly RequestSeriesPoint[];
}

export function RequestRateChart({ series }: RequestRateChartProps): JSX.Element {
  const data = series.map((p) => ({ ...p, ts: format(parseISO(p.t), 'HH:mm') }));
  const lastTotal = data[data.length - 1]?.v ?? 0;

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between space-y-0 pb-2">
        <div>
          <CardTitle>Request rate</CardTitle>
          <CardDescription>last 60 min · stacked by status class</CardDescription>
        </div>
        <div className="text-right">
          <p className="text-2xl font-semibold tabular-nums tracking-tight">{lastTotal}</p>
          <p className="text-xs text-muted-foreground">req / sec</p>
        </div>
      </CardHeader>
      <CardContent>
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: -16 }}>
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
              />
              <Tooltip
                contentStyle={{
                  background: 'hsl(var(--popover))',
                  color: 'hsl(var(--popover-foreground))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: 6,
                  fontSize: 12,
                }}
                labelClassName="text-xs"
              />
              <Legend
                verticalAlign="top"
                height={28}
                iconType="circle"
                iconSize={8}
                wrapperStyle={{ fontSize: 12 }}
              />
              <Area
                name="2xx"
                type="monotone"
                dataKey="ok"
                stackId="1"
                stroke={COLORS.ok}
                fill={COLORS.ok}
                fillOpacity={0.25}
                strokeWidth={1.5}
                isAnimationActive={false}
              />
              <Area
                name="4xx"
                type="monotone"
                dataKey="clientErr"
                stackId="1"
                stroke={COLORS.clientErr}
                fill={COLORS.clientErr}
                fillOpacity={0.4}
                strokeWidth={1.5}
                isAnimationActive={false}
              />
              <Area
                name="5xx"
                type="monotone"
                dataKey="serverErr"
                stackId="1"
                stroke={COLORS.serverErr}
                fill={COLORS.serverErr}
                fillOpacity={0.5}
                strokeWidth={1.5}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}
