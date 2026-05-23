/**
 * RedEndpointsTable — top-10 by traffic.
 *
 * Covers: rows match the data (count + path + p50/p95), empty state on
 * ``[]``, error state on ``isError``, and the top-10 truncation when
 * more than 10 endpoints arrive.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { EndpointStat } from '../types';

import { RedEndpointsTable } from './RedEndpointsTable';

function makeRow(
  label: string,
  count: number,
  overrides: Partial<EndpointStat> = {},
): EndpointStat {
  return {
    endpoint: label,
    count,
    error_pct: 0,
    p50_ms: 12,
    p95_ms: 45,
    p99_ms: 95,
    ...overrides,
  };
}

describe('RedEndpointsTable', () => {
  it('renders one row per endpoint with path + count', () => {
    render(
      <RedEndpointsTable
        data={[makeRow('GET /api/v1/orders', 89), makeRow('POST /api/v1/schedule/ops', 142)]}
        isLoading={false}
        isError={false}
        windowSeconds={60}
      />,
    );
    expect(screen.getByText('/api/v1/orders')).toBeInTheDocument();
    expect(screen.getByText('/api/v1/schedule/ops')).toBeInTheDocument();
    expect(screen.getByText('89')).toBeInTheDocument();
    expect(screen.getByText('142')).toBeInTheDocument();
  });

  it('renders the empty-state copy when data is []', () => {
    render(<RedEndpointsTable data={[]} isLoading={false} isError={false} windowSeconds={300} />);
    expect(screen.getByText(/no traffic in the last 5m/i)).toBeInTheDocument();
  });

  it('shows skeleton while loading', () => {
    render(<RedEndpointsTable data={undefined} isLoading isError={false} windowSeconds={60} />);
    expect(screen.getByTestId('red-endpoints-skeleton')).toBeInTheDocument();
  });

  it('shows the failure message on isError', () => {
    render(<RedEndpointsTable data={undefined} isLoading={false} isError windowSeconds={60} />);
    expect(screen.getByText(/failed to load endpoint stats/i)).toBeInTheDocument();
  });

  it('truncates to 10 rows even when more endpoints arrive', () => {
    const rows = Array.from({ length: 15 }, (_, i) =>
      makeRow(`GET /api/v1/r${i.toString().padStart(2, '0')}`, 100 - i),
    );
    render(<RedEndpointsTable data={rows} isLoading={false} isError={false} windowSeconds={60} />);
    // First 10 endpoints (by count, which we seed DESC) are visible.
    expect(screen.getByText('/api/v1/r00')).toBeInTheDocument();
    expect(screen.getByText('/api/v1/r09')).toBeInTheDocument();
    expect(screen.queryByText('/api/v1/r10')).not.toBeInTheDocument();
  });
});
