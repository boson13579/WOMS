/**
 * MetricCard — trend pill semantics + sparkline rendering.
 *
 * Trend math: pill should be UP-arrow rose when current > average and
 * DOWN-arrow emerald when current < average. The numeric delta shown in the
 * pill is the absolute difference, formatted to zero decimals.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ResourceMetric } from '../types';

import { MetricCard } from './MetricCard';

function metric(overrides: Partial<ResourceMetric> = {}): ResourceMetric {
  return {
    id: 'cpu',
    name: 'CPU',
    current: '23%',
    currentNumeric: 23,
    averageLabel: 'avg 18%',
    averageNumeric: 18,
    series: Array.from({ length: 10 }, (_, i) => ({
      t: new Date(Date.now() - i * 60_000).toISOString(),
      v: 20 + i,
    })),
    accent: 'emerald',
    ...overrides,
  };
}

describe('MetricCard', () => {
  it('shows metric name, current value, and average label', () => {
    render(<MetricCard metric={metric()} />);
    expect(screen.getByText('CPU')).toBeInTheDocument();
    expect(screen.getByText('23%')).toBeInTheDocument();
    expect(screen.getByText(/avg 18%/)).toBeInTheDocument();
  });

  it('renders a trending-up pill (rose tint) when current > average', () => {
    render(<MetricCard metric={metric({ currentNumeric: 30, averageNumeric: 20 })} />);
    // Delta is 10 (absolute) — find the pill containing "10".
    const pill = screen.getByText('10').closest('span');
    expect(pill).toHaveClass('bg-rose-50');
  });

  it('renders a trending-down pill (emerald tint) when current < average', () => {
    render(<MetricCard metric={metric({ currentNumeric: 10, averageNumeric: 25 })} />);
    const pill = screen.getByText('15').closest('span');
    expect(pill).toHaveClass('bg-emerald-50');
  });

  it('embeds a Recharts ResponsiveContainer for the sparkline', () => {
    render(<MetricCard metric={metric()} />);
    expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
  });
});
