/**
 * ObservabilityPage — full composition smoke test.
 *
 * Mocks ``global.fetch`` to return canonical RED + USE + SLO + Health
 * payloads, then asserts the page renders all four sections including
 * the headline KPI values and at least one row of the endpoints
 * table.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import type * as React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useRedHistoryStore } from '../stores/redHistoryStore';

import { ObservabilityPage } from './ObservabilityPage';

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => ({ id: 'u', username: 'alice', role: 'scheduler' }),
  useCurrentRole: () => 'scheduler',
}));

const HEALTH_RESPONSE = {
  services: [
    {
      id: 'api',
      name: 'API',
      status: 'healthy',
      summary: 'FastAPI · v0.1.0',
      details: [{ label: 'Version', value: '0.1.0' }],
    },
    {
      id: 'postgres',
      name: 'PostgreSQL',
      status: 'healthy',
      summary: 'postgres:15-alpine',
      details: [{ label: 'Latency', value: '2 ms' }],
    },
    {
      id: 'redis',
      name: 'Redis',
      status: 'healthy',
      summary: 'redis:7-alpine',
      details: [{ label: 'Latency', value: '1 ms' }],
    },
    {
      id: 'celery',
      name: 'Celery Worker',
      status: 'healthy',
      summary: 'Scheduler state=idle',
      details: [{ label: 'State', value: 'idle' }],
    },
  ],
};

const RED_RESPONSE = {
  window_seconds: 60,
  total_requests: 744,
  rate_per_sec: 12.4,
  error_count: 3,
  error_pct: 0.4,
  latency_ms: { p50: 12, p95: 45, p99: 95, max: 320 },
  by_endpoint: [
    {
      endpoint: 'POST /api/v1/orders',
      count: 142,
      error_pct: 0,
      p50_ms: 30,
      p95_ms: 80,
      p99_ms: 150,
    },
    {
      endpoint: 'GET /api/v1/orders',
      count: 89,
      error_pct: 0,
      p50_ms: 12,
      p95_ms: 28,
      p99_ms: 64,
    },
  ],
};

const RESOURCES_RESPONSE = {
  db_pool: { size: 5, checked_out: 2, overflow: 0, max_overflow: 10, utilization_pct: 13.3 },
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
};

const SLO_RESPONSE = {
  window_hours: 24,
  total_requests: 12_000,
  successful_requests: 11_990,
  success_pct: 99.92,
  slo_target_pct: 99.5,
  error_budget_pct_remaining: 84,
  error_budget_consumed_pct: 16,
  data_window_seconds_actual: 24 * 3600,
};

let qc: QueryClient;

function makeWrapper(children: React.ReactNode): JSX.Element {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return (
    <MemoryRouter>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </MemoryRouter>
  );
}

function setupFetchMock(): void {
  vi.mocked(global.fetch).mockImplementation((url) => {
    const target = String(url);
    if (target.startsWith('/api/v1/system/health')) {
      return Promise.resolve(new Response(JSON.stringify(HEALTH_RESPONSE), { status: 200 }));
    }
    if (target.startsWith('/api/v1/system/red')) {
      return Promise.resolve(new Response(JSON.stringify(RED_RESPONSE), { status: 200 }));
    }
    if (target.startsWith('/api/v1/system/resources')) {
      return Promise.resolve(new Response(JSON.stringify(RESOURCES_RESPONSE), { status: 200 }));
    }
    if (target.startsWith('/api/v1/system/slo')) {
      return Promise.resolve(new Response(JSON.stringify(SLO_RESPONSE), { status: 200 }));
    }
    return Promise.resolve(new Response('Not Found', { status: 404 }));
  });
}

describe('ObservabilityPage', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
    useRedHistoryStore.getState().reset();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    useRedHistoryStore.getState().reset();
    setupFetchMock();
  });

  it('renders the full RED + USE + Services + Endpoints composition', async () => {
    render(makeWrapper(<ObservabilityPage />));

    // Header
    expect(screen.getByRole('heading', { name: 'Observability' })).toBeInTheDocument();

    // Service health (uses dashboard's grid)
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'API' })).toBeInTheDocument();
    });

    // RED KPI headline numbers
    await waitFor(() => {
      expect(screen.getByText('12.40')).toBeInTheDocument(); // rate_per_sec
    });
    expect(screen.getByText('Rate')).toBeInTheDocument();
    expect(screen.getByText('Error rate')).toBeInTheDocument();
    expect(screen.getByText('P95 latency')).toBeInTheDocument();
    expect(screen.getByText('SLO compliance')).toBeInTheDocument();

    // USE section
    await waitFor(() => {
      expect(screen.getByText('DB connections')).toBeInTheDocument();
    });
    expect(screen.getByText('Workers')).toBeInTheDocument();

    // Endpoints — POST and GET share /api/v1/orders so there are two rows;
    // use ``getAllByText`` to assert both are visible.
    await waitFor(() => {
      expect(screen.getAllByText('/api/v1/orders').length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getByText('142')).toBeInTheDocument();
    expect(screen.getByText('89')).toBeInTheDocument();

    // SLO subtitle
    expect(screen.getByText(/target: 99.5%/i)).toBeInTheDocument();
  });

  it('renders the time-range selector with the default 1m active', async () => {
    render(makeWrapper(<ObservabilityPage />));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '1m' })).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: '1m' })).toHaveAttribute('aria-pressed', 'true');
  });
});
