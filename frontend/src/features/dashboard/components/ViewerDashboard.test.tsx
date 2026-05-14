/**
 * ViewerDashboard — what new (un-promoted) users see.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ViewerDashboard } from './ViewerDashboard';

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => ({ id: 'viewer-id', username: 'newbie', role: 'viewer' }),
  useCurrentRole: () => 'viewer',
}));

let qc: QueryClient;

function makeWrapper() {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  }
  return Wrapper;
}

describe('ViewerDashboard', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify({ services: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });

  it('greets the user by username', () => {
    render(<ViewerDashboard />, { wrapper: makeWrapper() });
    expect(screen.getByText(/newbie/)).toBeInTheDocument();
  });

  it('displays the current role', () => {
    render(<ViewerDashboard />, { wrapper: makeWrapper() });
    expect(screen.getByText(/viewer/i)).toBeInTheDocument();
  });

  it('shows a static "ask admin for elevation" notice', () => {
    render(<ViewerDashboard />, { wrapper: makeWrapper() });
    expect(screen.getByText(/contact.*administrator/i)).toBeInTheDocument();
  });
});
