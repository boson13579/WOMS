/**
 * PendingOpsTable — top-N rows + total footer.
 *
 * Verifies the truncation contract (N=10 by default), the per-row content,
 * the "queue empty" message, and the loading / error branches.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { PendingOpsEntry } from '../types';

import { PendingOpsTable } from './PendingOpsTable';

function makeEntry(rank: number): PendingOpsEntry {
  return {
    compound_id: `11111111-1111-1111-1111-${String(rank).padStart(12, '0')}`,
    rank,
    group: rank % 2 === 0 ? 'shrink' : 'grow',
    op_count: 2,
    ops: [
      {
        op: 'add',
        order_id: `22222222-2222-2222-2222-${String(rank).padStart(12, '0')}`,
        order_number: `ORD-20260512-${String(rank).padStart(4, '0')}`,
      },
      {
        op: 'remove',
        order_id: `22222222-2222-2222-2222-${String(rank).padStart(12, '0')}`,
        order_number: `ORD-20260512-${String(rank).padStart(4, '0')}`,
      },
    ],
    requested_by: '33333333-3333-3333-3333-333333333333',
  };
}

describe('PendingOpsTable', () => {
  it('renders all entries when fewer than the truncation cap', () => {
    const entries = [makeEntry(1), makeEntry(2), makeEntry(3)];
    render(<PendingOpsTable data={entries} isLoading={false} isError={false} />);

    expect(screen.getByText('ORD-20260512-0001')).toBeInTheDocument();
    expect(screen.getByText('ORD-20260512-0002')).toBeInTheDocument();
    expect(screen.getByText('ORD-20260512-0003')).toBeInTheDocument();
    expect(screen.getByText(/3 pending compound/)).toBeInTheDocument();
  });

  it('truncates to topN rows and shows "showing X of total"', () => {
    const entries = Array.from({ length: 1532 }, (_, i) => makeEntry(i + 1));
    render(<PendingOpsTable data={entries} isLoading={false} isError={false} />);

    // Rows: rank 1..10 visible, rank 11 not
    expect(screen.getByText('ORD-20260512-0001')).toBeInTheDocument();
    expect(screen.getByText('ORD-20260512-0010')).toBeInTheDocument();
    expect(screen.queryByText('ORD-20260512-0011')).not.toBeInTheDocument();
    // Footer hint
    expect(screen.getByText(/showing.*10.*of.*1,?532/i)).toBeInTheDocument();
  });

  it('shows the empty-queue message when data is an empty array', () => {
    render(<PendingOpsTable data={[]} isLoading={false} isError={false} />);
    expect(screen.getByText(/queue is empty/i)).toBeInTheDocument();
  });

  it('renders skeleton while loading', () => {
    render(<PendingOpsTable data={undefined} isLoading isError={false} />);
    expect(screen.getByTestId('pending-ops-skeleton')).toBeInTheDocument();
  });

  it('renders error message on isError', () => {
    render(<PendingOpsTable data={undefined} isLoading={false} isError />);
    expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
  });

  it('renders the rank and the group as badges', () => {
    render(<PendingOpsTable data={[makeEntry(7)]} isLoading={false} isError={false} />);
    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.getByText('grow')).toBeInTheDocument();
  });
});
