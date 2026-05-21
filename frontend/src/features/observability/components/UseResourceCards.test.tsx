/**
 * UseResourceCards — three cards (DB pool / Redis / Workers).
 *
 * Covers: rendering all three cards from a happy-path payload, the
 * workers-collapsed-by-default behaviour, the expand button revealing
 * the per-worker rows, the single-worker case (no expand button), and
 * the unreachable-state per section.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { UseResources } from '../types';

import { UseResourceCards } from './UseResourceCards';

function makeData(overrides: Partial<UseResources> = {}): UseResources {
  return {
    db_pool: {
      size: 5,
      checked_out: 2,
      overflow: 0,
      max_overflow: 10,
      utilization_pct: 13.3,
    },
    redis: {
      used_memory_bytes: 12_582_912,
      used_memory_peak_bytes: 25_165_824,
      connected_clients: 4,
      ops_per_sec: 87,
      evicted_keys: 0,
    },
    celery: {
      active_tasks: 3,
      queue_depth: 7,
      registered_workers: 3,
      workers: [
        { hostname: 'celery@worker-1', active_tasks: 2, status: 'active' },
        { hostname: 'celery@worker-2', active_tasks: 1, status: 'active' },
        { hostname: 'celery@worker-3', active_tasks: 0, status: 'idle' },
      ],
      truncated: false,
    },
    ...overrides,
  };
}

describe('UseResourceCards', () => {
  it('renders the three resource cards from a happy payload', () => {
    render(<UseResourceCards data={makeData()} isLoading={false} isError={false} />);
    expect(screen.getByText('DB connections')).toBeInTheDocument();
    expect(screen.getByText('Redis memory')).toBeInTheDocument();
    expect(screen.getByText('Workers')).toBeInTheDocument();
    expect(screen.getByText('2 / 15')).toBeInTheDocument(); // size + max_overflow
    expect(screen.getByText('3 up · 3 active')).toBeInTheDocument();
  });

  it('renders the workers card collapsed by default (drilldown list hidden)', () => {
    render(<UseResourceCards data={makeData()} isLoading={false} isError={false} />);
    // The toggle is visible, the list is not.
    expect(screen.getByRole('button', { name: /show per-worker/i })).toBeInTheDocument();
    expect(screen.queryByText('celery@worker-1')).not.toBeInTheDocument();
  });

  it('expands the workers list when the toggle is clicked', async () => {
    const user = userEvent.setup();
    render(<UseResourceCards data={makeData()} isLoading={false} isError={false} />);
    await user.click(screen.getByRole('button', { name: /show per-worker/i }));
    expect(screen.getByText('celery@worker-1')).toBeInTheDocument();
    expect(screen.getByText('celery@worker-2')).toBeInTheDocument();
    expect(screen.getByText('celery@worker-3')).toBeInTheDocument();
  });

  it('does NOT render the expand button when workers.length <= 1', () => {
    const single = makeData({
      celery: {
        active_tasks: 1,
        queue_depth: 0,
        registered_workers: 1,
        workers: [{ hostname: 'celery@worker-1', active_tasks: 1, status: 'active' }],
        truncated: false,
      },
    });
    render(<UseResourceCards data={single} isLoading={false} isError={false} />);
    expect(screen.queryByRole('button', { name: /show per-worker/i })).not.toBeInTheDocument();
  });

  it('renders the unreachable copy when a section is null', () => {
    const partial = makeData({ redis: null });
    render(<UseResourceCards data={partial} isLoading={false} isError={false} />);
    expect(screen.getByText(/redis unreachable/i)).toBeInTheDocument();
    // The other two cards still render normally.
    expect(screen.getByText('DB connections')).toBeInTheDocument();
    expect(screen.getByText('Workers')).toBeInTheDocument();
  });

  it('shows skeletons during the initial loading', () => {
    render(<UseResourceCards data={undefined} isLoading isError={false} />);
    expect(screen.getAllByTestId('use-resource-skeleton').length).toBeGreaterThanOrEqual(3);
  });

  it('shows the global failure message when the whole fetch errors', () => {
    render(<UseResourceCards data={undefined} isLoading={false} isError />);
    expect(screen.getByText(/failed to load resources/i)).toBeInTheDocument();
  });
});
