/**
 * DashboardPage — high-level assembly + refresh interaction.
 *
 * Covers:
 *   1. Renders a loading skeleton before data resolves.
 *   2. Renders all four content sections after data loads.
 *   3. Exposes the right widget counts.
 *   4. The refresh button triggers a refetch (button briefly disables).
 *
 * The page is mounted inside a fresh `QueryClientProvider` per test so query
 * caches don't leak between specs, and inside a `MemoryRouter` because the
 * sidebar — though not part of the page — uses `<Link>` and `useLocation`.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DashboardPage } from './DashboardPage';

function makeWrapper(): { wrapper: ({ children }: { children: ReactNode }) => JSX.Element } {
  // Disable retries so any throw surfaces immediately in tests.
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  function Wrapper({ children }: { children: ReactNode }): JSX.Element {
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }
  return { wrapper: Wrapper };
}

describe('DashboardPage', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows a skeleton placeholder while data is loading', () => {
    const { wrapper: Wrapper } = makeWrapper();
    render(<DashboardPage />, { wrapper: Wrapper });
    expect(screen.getByTestId('dashboard-skeleton')).toBeInTheDocument();
  });

  it('renders all four content sections after data loads', async () => {
    const { wrapper: Wrapper } = makeWrapper();
    render(<DashboardPage />, { wrapper: Wrapper });

    // Drive the simulated 200 ms fetcher delay.
    await vi.advanceTimersByTimeAsync(300);

    await waitFor(() => {
      expect(screen.getByRole('region', { name: /service health/i })).toBeInTheDocument();
    });
    expect(screen.getByRole('region', { name: /resource utilization/i })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /application metrics/i })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /business metrics/i })).toBeInTheDocument();
  });

  it('shows four service status cards, four metric cards, and an Orders Today card', async () => {
    const { wrapper: Wrapper } = makeWrapper();
    render(<DashboardPage />, { wrapper: Wrapper });

    await vi.advanceTimersByTimeAsync(300);
    await waitFor(() => screen.getByRole('region', { name: /service health/i }));

    const services = screen.getByRole('region', { name: /service health/i });
    expect(within(services).getAllByRole('heading', { level: 3 })).toHaveLength(4);

    const resources = screen.getByRole('region', { name: /resource utilization/i });
    expect(within(resources).getByText('CPU')).toBeInTheDocument();
    expect(within(resources).getByText('Memory')).toBeInTheDocument();
    expect(within(resources).getByText('Disk')).toBeInTheDocument();
    expect(within(resources).getByText('Network I/O')).toBeInTheDocument();

    const business = screen.getByRole('region', { name: /business metrics/i });
    expect(within(business).getByText(/orders today/i)).toBeInTheDocument();
    expect(within(business).getByText(/recent activity/i)).toBeInTheDocument();
  });

  it('displays the "mock data" disclaimer in the header', () => {
    const { wrapper: Wrapper } = makeWrapper();
    render(<DashboardPage />, { wrapper: Wrapper });
    expect(screen.getByText(/mock data/i)).toBeInTheDocument();
  });

  it('disables the refresh button while a refetch is in flight', async () => {
    const { wrapper: Wrapper } = makeWrapper();
    render(<DashboardPage />, { wrapper: Wrapper });

    // Wait for the initial query to settle so the button is enabled.
    await vi.advanceTimersByTimeAsync(300);
    await waitFor(() => screen.getByRole('region', { name: /service health/i }));

    const refresh = screen.getByRole('button', { name: /refresh/i });
    expect(refresh).not.toBeDisabled();

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(refresh);

    // While the simulated fetcher is in flight `isFetching` flips to true.
    await waitFor(() => {
      expect(refresh).toBeDisabled();
    });

    await vi.advanceTimersByTimeAsync(300);
    await waitFor(() => {
      expect(refresh).not.toBeDisabled();
    });
  });
});
