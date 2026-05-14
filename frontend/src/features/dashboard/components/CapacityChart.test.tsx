/**
 * CapacityChart — 30-day prefix-sum area chart.
 *
 * Recharts' ResponsiveContainer is stubbed by the test setup (see
 * src/test/setup.ts) so the chart actually mounts in jsdom; we assert
 * structural / textual content rather than pixel-perfect output.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ScheduleCapacityResponse } from '../types';

import { CapacityChart } from './CapacityChart';

function makeResponse(): ScheduleCapacityResponse {
  return {
    base_date: '2026-05-12',
    daily_capacity: 10_000,
    entries: Array.from({ length: 30 }, (_, i) => ({
      date: new Date(2026, 4, 12 + i).toISOString().slice(0, 10),
      cumulative_remaining: (i + 1) * 9000,
    })),
  };
}

describe('CapacityChart', () => {
  it('renders the chart shell with title', () => {
    render(<CapacityChart data={makeResponse()} isLoading={false} isError={false} />);
    expect(screen.getByText(/capacity/i)).toBeInTheDocument();
    expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
  });

  it('shows the last-day cumulative_remaining as the headline number', () => {
    render(<CapacityChart data={makeResponse()} isLoading={false} isError={false} />);
    // 30th entry: (30) * 9000 = 270_000
    expect(screen.getByText(/270,?000/)).toBeInTheDocument();
  });

  it('renders skeleton while loading', () => {
    render(<CapacityChart data={undefined} isLoading isError={false} />);
    expect(screen.getByTestId('capacity-chart-skeleton')).toBeInTheDocument();
  });

  it('renders error message on isError', () => {
    render(<CapacityChart data={undefined} isLoading={false} isError />);
    expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
  });

  it('handles empty entries gracefully (e.g. fresh Redis)', () => {
    render(
      <CapacityChart
        data={{ base_date: '2026-05-12', daily_capacity: 10_000, entries: [] }}
        isLoading={false}
        isError={false}
      />,
    );
    // Headline reads 0 when there's no entries; component must NOT crash.
    expect(screen.getByText(/no capacity data/i)).toBeInTheDocument();
  });
});
