/**
 * UseResourceCards — the three-card USE row.
 *
 * Verifies the render branches added in the observability revamp:
 *   - DB connections per-replica caption (multi-pod vs single-pod)
 *   - Redis caption swap on ``max_memory_bytes`` (capped vs uncapped)
 *   - Redis hides the saturation bar when uncapped
 *   - Live Connections card renders total + per-replica
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { UseResources } from '../types';

import { UseResourceCards } from './UseResourceCards';

function makeData(over: Partial<UseResources> = {}): UseResources {
  return {
    db_pool: {
      size: 50,
      checked_out: 8,
      overflow: 0,
      max_overflow: 0,
      utilization_pct: 16.0,
      replicas: [],
    },
    redis: {
      used_memory_bytes: 1_048_576,
      used_memory_peak_bytes: 2_097_152,
      max_memory_bytes: 0,
      connected_clients: 3,
      ops_per_sec: 5,
      evicted_keys: 0,
    },
    celery: null,
    ws_connections: { total: 0, replicas: [] },
    ...over,
  };
}

describe('UseResourceCards', () => {
  it('DB connections shows the size/checked_out value plus a utilization detail', () => {
    const data = makeData();
    render(<UseResourceCards data={data} isLoading={false} isError={false} />);
    // Headline: "<checked_out> / <size+overflow>"
    expect(screen.getByText('8 / 50')).toBeInTheDocument();
    // Detail next to value: "(16.0% used)"
    expect(screen.getByText(/16.0%\s*used/)).toBeInTheDocument();
  });

  it('DB connections single-replica deployments omit the per-replica caption', () => {
    const data = makeData({
      db_pool: {
        size: 50,
        checked_out: 8,
        overflow: 0,
        max_overflow: 0,
        utilization_pct: 16.0,
        replicas: [
          { pod_id: 'solo-pod', size: 50, checked_out: 8, overflow: 0, max_overflow: 0 },
        ],
      },
    });
    render(<UseResourceCards data={data} isLoading={false} isError={false} />);
    // Single replica → no per-replica line
    expect(screen.queryByText(/solo-pod/)).not.toBeInTheDocument();
  });

  it('DB connections multi-replica rendering shows each pod_id slice on the caption line', () => {
    const data = makeData({
      db_pool: {
        size: 50,
        checked_out: 12,
        overflow: 0,
        max_overflow: 0,
        utilization_pct: 24.0,
        replicas: [
          { pod_id: 'backend-aaaaaa', size: 25, checked_out: 7, overflow: 0, max_overflow: 0 },
          { pod_id: 'backend-bbbbbb', size: 25, checked_out: 5, overflow: 0, max_overflow: 0 },
        ],
      },
    });
    render(<UseResourceCards data={data} isLoading={false} isError={false} />);
    expect(screen.getByText(/aaaaaa: 7 \/ 25/)).toBeInTheDocument();
    expect(screen.getByText(/bbbbbb: 5 \/ 25/)).toBeInTheDocument();
  });

  it('Redis uncapped: shows "no cap" + client count on TWO separate caption lines, NO bar', () => {
    const data = makeData({
      redis: {
        used_memory_bytes: 5_000_000,
        used_memory_peak_bytes: 5_000_000,
        max_memory_bytes: 0,
        connected_clients: 4,
        ops_per_sec: 2,
        evicted_keys: 0,
      },
    });
    render(<UseResourceCards data={data} isLoading={false} isError={false} />);
    expect(screen.getByText('no cap')).toBeInTheDocument();
    expect(screen.getByText(/4 clients/)).toBeInTheDocument();
    // No dashed-placeholder bar when explicitly hidden
    expect(screen.queryByTestId('util-bar-dashed')).not.toBeInTheDocument();
  });

  it('Redis capped: combines cap + clients on ONE line and renders the saturation bar', () => {
    const data = makeData({
      redis: {
        used_memory_bytes: 5_000_000,
        used_memory_peak_bytes: 5_000_000,
        max_memory_bytes: 100_000_000,
        connected_clients: 7,
        ops_per_sec: 1,
        evicted_keys: 0,
      },
    });
    render(<UseResourceCards data={data} isLoading={false} isError={false} />);
    expect(screen.getByText(/cap .*7 clients/)).toBeInTheDocument();
    // DB pool also renders a bar in the default fixture; just assert
    // that the Redis card contributed at least one fill bar (we'd be
    // here with zero if hideBar wrongly fired).
    expect(screen.queryAllByTestId('util-bar-fill').length).toBeGreaterThanOrEqual(2);
  });

  it('Redis surfaces eviction count only when > 0', () => {
    const data = makeData({
      redis: {
        used_memory_bytes: 1_000_000,
        used_memory_peak_bytes: 1_000_000,
        max_memory_bytes: 0,
        connected_clients: 1,
        ops_per_sec: 0,
        evicted_keys: 42,
      },
    });
    render(<UseResourceCards data={data} isLoading={false} isError={false} />);
    expect(screen.getByText(/42 evicted/)).toBeInTheDocument();
  });

  it('Live Connections renders the total and per-replica breakdown', () => {
    const data = makeData({
      ws_connections: {
        total: 9,
        replicas: [
          { pod_id: 'backend-aaaaaa', count: 5 },
          { pod_id: 'backend-bbbbbb', count: 4 },
        ],
      },
    });
    render(<UseResourceCards data={data} isLoading={false} isError={false} />);
    expect(screen.getByText('Live connections')).toBeInTheDocument();
    expect(screen.getByText('9')).toBeInTheDocument();
    expect(screen.getByText(/aaaaaa: 5/)).toBeInTheDocument();
    expect(screen.getByText(/bbbbbb: 4/)).toBeInTheDocument();
  });

  it('Live Connections card has no saturation bar (no natural denominator)', () => {
    const data = makeData({
      ws_connections: { total: 3, replicas: [] },
    });
    render(<UseResourceCards data={data} isLoading={false} isError={false} />);
    // Three cards render; assert by total-bar count under-or-equal 2
    // (DB has a bar in this fixture; Redis without cap doesn't; WS doesn't).
    const fills = screen.queryAllByTestId('util-bar-fill');
    const dashed = screen.queryAllByTestId('util-bar-dashed');
    expect(fills.length + dashed.length).toBeLessThanOrEqual(1);
  });
});
