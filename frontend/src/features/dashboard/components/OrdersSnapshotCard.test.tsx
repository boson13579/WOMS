/**
 * OrdersSnapshotCard — 4 tiles, one per OrderStatus.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { OrdersSnapshotCounts } from '../types';

import { OrdersSnapshotCard } from './OrdersSnapshotCard';

function makeData(overrides: Partial<OrdersSnapshotCounts> = {}): OrdersSnapshotCounts {
  return {
    pending: 3,
    scheduled: 96,
    in_production: 12,
    completed: 65,
    ...overrides,
  };
}

describe('OrdersSnapshotCard', () => {
  it('renders all four status counts with their labels', () => {
    render(<OrdersSnapshotCard data={makeData()} isLoading={false} isError={false} />);
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('96')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('65')).toBeInTheDocument();
    expect(screen.getByText(/pending/i)).toBeInTheDocument();
    expect(screen.getByText(/scheduled/i)).toBeInTheDocument();
    expect(screen.getByText(/in production/i)).toBeInTheDocument();
    expect(screen.getByText(/completed/i)).toBeInTheDocument();
  });

  it('renders skeleton while loading', () => {
    render(<OrdersSnapshotCard data={undefined} isLoading isError={false} />);
    expect(screen.getByTestId('orders-snapshot-skeleton')).toBeInTheDocument();
  });

  it('renders error message on isError', () => {
    render(<OrdersSnapshotCard data={undefined} isLoading={false} isError />);
    expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
  });

  it('formats large counts with thousands separators', () => {
    render(
      <OrdersSnapshotCard data={makeData({ scheduled: 1532 })} isLoading={false} isError={false} />,
    );
    expect(screen.getByText('1,532')).toBeInTheDocument();
  });
});
