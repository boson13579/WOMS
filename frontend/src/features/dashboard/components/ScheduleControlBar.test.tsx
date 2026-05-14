/**
 * ScheduleControlBar — Trigger Scheduling / Rebuild buttons.
 *
 * Role-gated UI: the bar renders nothing for viewer / order_manager and
 * shows both buttons for scheduler / root. Backend re-checks via 403; the
 * frontend hide is purely UX.
 *
 * Mutation calls go through the matching API hooks; tests verify the
 * fetch URLs + methods rather than the React Query plumbing itself.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ScheduleControlBar } from './ScheduleControlBar';

const mockRole = { value: 'scheduler' as string | null };
vi.mock('@/lib/auth', () => ({
  useCurrentRole: () => mockRole.value,
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
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  }
  return Wrapper;
}

describe('ScheduleControlBar', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    mockRole.value = 'scheduler';
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify({ task_id: 'task-mock', message: 'Scheduling started' }), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });

  it('renders the trigger + rebuild buttons for scheduler role', () => {
    render(<ScheduleControlBar />, { wrapper: makeWrapper() });
    expect(screen.getByRole('button', { name: /trigger/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /rebuild/i })).toBeInTheDocument();
  });

  it('renders the buttons for root role', () => {
    mockRole.value = 'root';
    render(<ScheduleControlBar />, { wrapper: makeWrapper() });
    expect(screen.getByRole('button', { name: /trigger/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /rebuild/i })).toBeInTheDocument();
  });

  it('renders nothing for order_manager role', () => {
    mockRole.value = 'order_manager';
    const { container } = render(<ScheduleControlBar />, { wrapper: makeWrapper() });
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing for viewer role', () => {
    mockRole.value = 'viewer';
    const { container } = render(<ScheduleControlBar />, { wrapper: makeWrapper() });
    expect(container.firstChild).toBeNull();
  });

  it('POSTs /api/v1/schedule/trigger when Trigger is clicked', async () => {
    render(<ScheduleControlBar />, { wrapper: makeWrapper() });
    fireEvent.click(screen.getByRole('button', { name: /trigger/i }));
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/v1/schedule/trigger',
        expect.objectContaining({ method: 'POST', credentials: 'include' }),
      );
    });
  });

  it('POSTs /api/v1/schedule/rebuild when Rebuild is clicked', async () => {
    render(<ScheduleControlBar />, { wrapper: makeWrapper() });
    fireEvent.click(screen.getByRole('button', { name: /rebuild/i }));
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/v1/schedule/rebuild',
        expect.objectContaining({ method: 'POST', credentials: 'include' }),
      );
    });
  });
});
