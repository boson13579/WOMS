/**
 * DashboardPage — role-based composition.
 *
 * Verifies the page picks the right composition per role:
 *   * viewer / unauthenticated → ViewerDashboard (welcome + service health)
 *   * order_manager / scheduler / root → full operational dashboard
 *
 * Individual widget behaviour is tested in their own `*.test.tsx`; this
 * file only proves the assembly is wired correctly. The Header sits
 * inside <MemoryRouter> because it uses react-router's useNavigate.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import type * as React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DashboardPage } from './DashboardPage';

const mockRole = { value: 'order_manager' as string | null };
const mockUser = {
  value: { id: 'u', username: 'alice', role: 'order_manager' } as {
    id: string;
    username: string;
    role: string;
  } | null,
};

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => mockUser.value,
  useCurrentRole: () => mockRole.value,
  useCurrentUserId: () => mockUser.value?.id ?? null,
}));

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    loading: vi.fn(),
    info: vi.fn(),
  },
}));

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

/** Configure fetch mock to answer the dashboard's various endpoints. */
function setupDefaultFetchMock(): void {
  vi.mocked(global.fetch).mockImplementation((url) => {
    const u = new URL(String(url), 'http://localhost');
    if (u.pathname === '/api/v1/system/health') {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            services: [
              { id: 'api', name: 'API', status: 'healthy', summary: 'ok', details: [] },
              {
                id: 'postgres',
                name: 'PostgreSQL',
                status: 'healthy',
                summary: 'ok',
                details: [],
              },
              { id: 'redis', name: 'Redis', status: 'healthy', summary: 'ok', details: [] },
              {
                id: 'celery',
                name: 'Celery Worker',
                status: 'healthy',
                summary: 'ok',
                details: [],
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );
    }
    if (u.pathname === '/api/v1/schedule/status') {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            state: 'idle',
            started_at: null,
            finished_at: null,
            task_id: null,
            error: null,
            message: 'No scheduling has been run yet',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );
    }
    if (u.pathname === '/api/v1/schedule/capacity') {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            base_date: '2026-05-12',
            daily_capacity: 10_000,
            entries: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );
    }
    if (u.pathname === '/api/v1/schedule/pending-ops') {
      return Promise.resolve(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    if (u.pathname === '/api/v1/orders') {
      return Promise.resolve(
        new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 1 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    return Promise.resolve(new Response('Not Found', { status: 404 }));
  });
}

describe('DashboardPage', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    mockRole.value = 'order_manager';
    mockUser.value = { id: 'u', username: 'alice', role: 'order_manager' };
    setupDefaultFetchMock();
  });

  it('order_manager sees the full operational dashboard', async () => {
    render(<DashboardPage />, { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(screen.getByRole('region', { name: /capacity/i })).toBeInTheDocument();
    });
    expect(screen.getByRole('region', { name: /queue and orders/i })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /service health/i })).toBeInTheDocument();
  });

  it('order_manager does NOT see the Trigger / Rebuild buttons', () => {
    render(<DashboardPage />, { wrapper: makeWrapper() });
    expect(screen.queryByRole('button', { name: /trigger/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /rebuild/i })).not.toBeInTheDocument();
  });

  it('scheduler sees Trigger / Rebuild buttons', async () => {
    mockRole.value = 'scheduler';
    mockUser.value = { id: 'u', username: 'alice', role: 'scheduler' };
    render(<DashboardPage />, { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /trigger/i })).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /rebuild/i })).toBeInTheDocument();
  });

  it('viewer sees the ViewerDashboard (no schedule widgets)', () => {
    mockRole.value = 'viewer';
    mockUser.value = { id: 'u', username: 'newbie', role: 'viewer' };
    render(<DashboardPage />, { wrapper: makeWrapper() });
    expect(screen.getByText(/welcome.*newbie/i)).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: /capacity/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: /queue and orders/i })).not.toBeInTheDocument();
  });
});
