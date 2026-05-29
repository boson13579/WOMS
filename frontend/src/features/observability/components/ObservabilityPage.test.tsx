/**
 * ObservabilityPage — page composition + header wiring.
 *
 * Covers:
 *  - Renders all four sections (Services, RED, USE, Top endpoints) with the
 *    correct aria-label.
 *  - Header subtitle reflects the active window pill (e.g. "last 1m" → "last
 *    5m" after a click).
 *  - Time-range pill click swaps the active value (and the RED query refires
 *    with the new ``window_seconds``).
 *  - Degraded banner appears when EITHER red OR lag carry ``data_status:
 *    'degraded'`` and stays hidden otherwise.
 *  - "Updated …" label appears once at least one of the three RED/USE/Lag
 *    queries has resolved (i.e. ``dataUpdatedAt > 0``), not before.
 *  - Refresh button invalidates the four observability query prefixes via
 *    React Query.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type * as React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ObservabilityPage } from './ObservabilityPage';

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => ({ id: 'u', username: 'alice', role: 'scheduler' }),
  useCurrentRole: () => 'scheduler',
  useCurrentUserId: () => 'u',
}));

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    loading: vi.fn(),
    info: vi.fn(),
  },
}));

interface FetchOverrides {
  redDataStatus?: 'ok' | 'degraded';
  lagDataStatus?: 'ok' | 'degraded';
}

const VALID_HEALTH = {
  services: [
    { id: 'api', name: 'API', status: 'healthy', summary: 'ok', details: [] },
    { id: 'postgres', name: 'PostgreSQL', status: 'healthy', summary: 'ok', details: [] },
    { id: 'redis', name: 'Redis', status: 'healthy', summary: 'ok', details: [] },
    { id: 'celery', name: 'Celery Worker', status: 'healthy', summary: 'ok', details: [] },
  ],
};

function makeRedResponse(status: 'ok' | 'degraded' = 'ok'): unknown {
  return {
    window_seconds: 60,
    total_requests: 200,
    rate_per_sec: 3.3,
    error_count: 1,
    error_pct: 0.5,
    latency_ms: { p50: 12, p95: 45, p99: 95, max: 320 },
    by_endpoint: [
      {
        endpoint: 'GET /api/v1/orders',
        count: 142,
        error_pct: 0,
        p50_ms: 11,
        p95_ms: 33,
        p99_ms: 80,
      },
    ],
    data_status: status,
  };
}

function makeLagResponse(status: 'ok' | 'degraded' = 'ok'): unknown {
  return {
    window_seconds: 60,
    sample_count: 12,
    p50_ms: 30,
    p95_ms: 250,
    max_ms: 400,
    data_status: status,
  };
}

const VALID_RESOURCES = {
  db_pool: {
    size: 5,
    checked_out: 1,
    overflow: 0,
    max_overflow: 10,
    utilization_pct: 20,
    replicas: [],
  },
  redis: {
    used_memory_bytes: 1_000_000,
    used_memory_peak_bytes: 2_000_000,
    max_memory_bytes: 0,
    connected_clients: 4,
    ops_per_sec: 10,
    evicted_keys: 0,
  },
  celery: {
    active_tasks: 0,
    queue_depth: 0,
    registered_workers: 1,
    workers: [],
    truncated: false,
  },
  ws_connections: { total: 0, replicas: [] },
};

function setupFetchMock(overrides: FetchOverrides = {}): void {
  vi.mocked(global.fetch).mockImplementation((url) => {
    const u = new URL(String(url), 'http://localhost');
    const json = (body: unknown): Promise<Response> =>
      Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );

    if (u.pathname === '/api/v1/system/health') return json(VALID_HEALTH);
    if (u.pathname === '/api/v1/system/red') return json(makeRedResponse(overrides.redDataStatus));
    if (u.pathname === '/api/v1/system/resources') return json(VALID_RESOURCES);
    if (u.pathname === '/api/v1/system/schedule-lag')
      return json(makeLagResponse(overrides.lagDataStatus));
    // Header pulls unread notifications; return an empty envelope so the
    // bell badge logic doesn't blow up.
    if (u.pathname === '/api/v1/notifications')
      return json({ items: [], total: 0, page: 1, page_size: 50 });

    return Promise.resolve(new Response('Not Found', { status: 404 }));
  });
}

let qc: QueryClient;

function makeWrapper() {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={qc}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }
  return Wrapper;
}

describe('ObservabilityPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupFetchMock();
  });

  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  it('renders the four labelled sections', async () => {
    render(<ObservabilityPage />, { wrapper: makeWrapper() });
    expect(screen.getByRole('region', { name: /service health/i })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /red metrics/i })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /use resources/i })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /top endpoints/i })).toBeInTheDocument();
    // Wait for at least one fetch to settle so React Query doesn't leak
    // pending work into the next test.
    await waitFor(() => {
      expect(screen.getByText(/\/api\/v1\/orders/i)).toBeInTheDocument();
    });
  });

  it('default subtitle reads "last 1m" (60s default window)', () => {
    render(<ObservabilityPage />, { wrapper: makeWrapper() });
    expect(screen.getByText(/RED \+ USE · last 1m/i)).toBeInTheDocument();
  });

  it('changing the time-range pill updates the subtitle window label', async () => {
    const user = userEvent.setup();
    render(<ObservabilityPage />, { wrapper: makeWrapper() });

    // 1m → 5m → 15m → 1h covers all three branches of formatWindowLabel
    // (seconds, minutes, hours).
    await user.click(screen.getByRole('button', { name: '5m' }));
    expect(screen.getByText(/RED \+ USE · last 5m/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '15m' }));
    expect(screen.getByText(/RED \+ USE · last 15m/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '1h' }));
    expect(screen.getByText(/RED \+ USE · last 1h/i)).toBeInTheDocument();
  });

  it('switching the window pill refires the RED query with the new window_seconds', async () => {
    const user = userEvent.setup();
    render(<ObservabilityPage />, { wrapper: makeWrapper() });

    // Wait for initial RED fetch (window=60).
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/v1/system/red?window_seconds=60',
        expect.objectContaining({ credentials: 'include' }),
      );
    });

    await user.click(screen.getByRole('button', { name: '5m' }));
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/v1/system/red?window_seconds=300',
        expect.objectContaining({ credentials: 'include' }),
      );
    });
  });

  it('hides the degraded banner when both red and lag report data_status: ok', async () => {
    render(<ObservabilityPage />, { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(screen.getByText(/\/api\/v1\/orders/i)).toBeInTheDocument();
    });
    expect(screen.queryByTestId('metrics-degraded-banner')).not.toBeInTheDocument();
  });

  it('shows the degraded banner when RED is degraded', async () => {
    setupFetchMock({ redDataStatus: 'degraded' });
    render(<ObservabilityPage />, { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(screen.getByTestId('metrics-degraded-banner')).toBeInTheDocument();
    });
    expect(screen.getByTestId('metrics-degraded-banner')).toHaveTextContent(
      /metrics data is currently unavailable/i,
    );
  });

  it('shows the degraded banner when only lag is degraded', async () => {
    setupFetchMock({ lagDataStatus: 'degraded' });
    render(<ObservabilityPage />, { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(screen.getByTestId('metrics-degraded-banner')).toBeInTheDocument();
    });
  });

  it('renders the "Updated …" label once at least one metrics query has resolved', async () => {
    render(<ObservabilityPage />, { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(screen.getByText(/^Updated /)).toBeInTheDocument();
    });
  });

  it('clicking Refresh invalidates the four observability query prefixes', async () => {
    const user = userEvent.setup();
    render(<ObservabilityPage />, { wrapper: makeWrapper() });

    // Let initial queries settle so the cache is populated.
    await waitFor(() => {
      expect(screen.getByText(/\/api\/v1\/orders/i)).toBeInTheDocument();
    });

    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    await user.click(screen.getByRole('button', { name: /refresh metrics/i }));

    // Each of the four observability prefixes must be invalidated. The
    // page also drives ``system/health`` through this same button so we
    // include it in the expected list.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['system', 'red'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['system', 'resources'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['system', 'schedule-lag'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['system', 'health'] });
    expect(invalidateSpy).toHaveBeenCalledTimes(4);
  });

  it('renders the top-endpoints row data when RED resolves', async () => {
    render(<ObservabilityPage />, { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(screen.getByText(/\/api\/v1\/orders/i)).toBeInTheDocument();
    });
    // count "142" from the seeded endpoint stat
    expect(screen.getByText('142')).toBeInTheDocument();
  });
});
