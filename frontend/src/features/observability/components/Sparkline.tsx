/**
 * Sparkline — fixed-height tiny area chart for the RED KPI cards.
 *
 * Reads from the parent-provided ring buffer (last 20 polls) and renders
 * a minimal Recharts `AreaChart`: no axes, no grid, no tooltip, full
 * width, ~56 px tall. Per the plan, the component **early-returns null**
 * when the buffer has fewer than 3 samples so the empty/warming-up
 * period reads as "no chart yet" rather than a 1-pixel artifact and the
 * card layout doesn't jitter.
 *
 * Reuses the same linear-gradient pattern as ``CapacityChart`` so the
 * primary fill colour automatically tracks the theme.
 */
import { useId } from 'react';
import { Area, AreaChart, ResponsiveContainer } from 'recharts';

const MIN_POINTS_TO_RENDER = 3;
const HEIGHT_PX = 56;

interface SparklineProps {
  /** Series of polled metric values, oldest first. */
  values: number[];
  /** Optional ARIA label for the chart container. */
  ariaLabel?: string | undefined;
}

export function Sparkline({ values, ariaLabel }: Readonly<SparklineProps>): JSX.Element | null {
  const gradientId = useId();

  // Warming-up guard: rendering an AreaChart with 0 / 1 / 2 points draws
  // a single flat pixel that visually reads as broken. Empty until we
  // have enough samples for a real slope.
  if (values.length < MIN_POINTS_TO_RENDER) {
    return null;
  }

  // Recharts wants ``[{ v: 1 }, { v: 2 }, …]``; the ring buffer is just
  // a number[] so we wrap into the minimum shape here.
  const data = values.map((v, i) => ({ i, v }));

  return (
    <div
      data-testid="sparkline"
      aria-label={ariaLabel}
      style={{ height: HEIGHT_PX }}
      className="w-full"
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 0, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.35} />
              <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="v"
            stroke="hsl(var(--primary))"
            strokeWidth={1.5}
            fill={`url(#${gradientId})`}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
