/**
 * AuditPage — integration: header, URL-sync, refresh, role gate.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AuditPage } from './AuditPage';

const mockAuth = { role: 'root' as 'root' | 'scheduler' | 'order_manager' | 'viewer' | null };

vi.mock('@/lib/auth', () => ({
  useCurrentRole: () => mockAuth.role,
  useCurrentUser: () => ({ id: 'u', username: 'admin', role: mockAuth.role }),
}));

vi.mock('@/features/auth/stores/authStore', () => ({
  useAuthStore: () => () => Promise.resolve(),
}));

vi.mock('@/components/layout/mobileNavStore', () => ({
  useMobileNavStore: () => () => undefined,
}));

const mockUsers = vi.fn();
vi.mock('@/features/users/api/users', () => ({
  // eslint-disable-next-line @typescript-eslint/no-unsafe-return
  listUsers: (search?: string) => mockUsers(search),
}));

vi.mock('@/features/users/api/useUsernames', () => ({
  useUsernames: () => ({ data: {} }),
}));

let qc: QueryClient;

// Capture the current location so URL-sync assertions can read it.
let lastLocation: { pathname: string; search: string } = { pathname: '', search: '' };
function LocationProbe() {
  const location = useLocation();
  lastLocation = { pathname: location.pathname, search: location.search };
  return null;
}

function renderPage(initialEntry = '/audit') {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="/audit"
            element={
              <>
                <LocationProbe />
                <AuditPage />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const SAMPLE_RESPONSE = {
  items: [
    {
      id: '11111111-1111-1111-1111-111111111111',
      action: 'user.login_succeeded',
      user_id: '22222222-2222-2222-2222-222222222222',
      resource_id: '33333333-3333-3333-3333-333333333333',
      old_value: null,
      new_value: { ip: '127.0.0.1' },
      created_at: '2026-05-20T14:32:10Z',
    },
  ],
  total: 1,
  page: 1,
  page_size: 20,
};

function mockEventsFetch(body: unknown = SAMPLE_RESPONSE, status = 200): void {
  // Reset and re-bind global.fetch to a context-aware handler so we don't
  // collide with the default 404 fallback from src/test/setup.ts.
  vi.mocked(global.fetch).mockImplementation((url: RequestInfo | URL) => {
    let u: string;
    if (typeof url === 'string') {
      u = url;
    } else if (url instanceof URL) {
      u = url.toString();
    } else {
      u = url.url;
    }
    if (u.startsWith('/api/v1/audit/events')) {
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    if (u.startsWith('/api/v1/users')) {
      return Promise.resolve(
        new Response(JSON.stringify({ users: [], total: 0 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    return Promise.resolve(new Response('Not Found', { status: 404 }));
  });
}

describe('AuditPage', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
    mockAuth.role = 'root';
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mockUsers.mockResolvedValue({ users: [], total: 0 });
  });

  it('renders the Header with title "Audit Log" for root users', () => {
    mockEventsFetch();
    renderPage();
    expect(screen.getByRole('heading', { level: 1, name: /Audit Log/i })).toBeInTheDocument();
  });

  it('renders an access-denied card for non-root users', () => {
    mockAuth.role = 'order_manager';
    mockEventsFetch();
    renderPage();
    expect(screen.getByText(/Root access required/i)).toBeInTheDocument();
  });

  it('hydrates filter values from URL search params on mount', async () => {
    mockEventsFetch();
    renderPage('/audit?action=order.created&resource_type=order&page=2&page_size=50');

    // Filter inputs should reflect URL.
    await waitFor(() => {
      const actionInput = screen.getByLabelText(/Action filter/i);
      expect((actionInput as HTMLInputElement).value).toBe('order.created');
    });
    const resourceSelect = screen.getByLabelText(/Resource type filter/i);
    expect((resourceSelect as HTMLSelectElement).value).toBe('order');
  });

  it('updates URL on resource-type change', async () => {
    mockEventsFetch();
    const user = userEvent.setup();
    renderPage();

    await user.selectOptions(screen.getByLabelText(/Resource type filter/i), 'schedule');

    await waitFor(() => {
      expect(lastLocation.search).toContain('resource_type=schedule');
    });
  });

  it('clears URL params when Clear filters is pressed', async () => {
    mockEventsFetch();
    const user = userEvent.setup();
    renderPage('/audit?action=order.updated&page=2');

    await user.click(screen.getByRole('button', { name: /Clear filters/i }));

    await waitFor(() => {
      expect(lastLocation.search).toBe('');
    });
  });

  it('renders event rows when data lands', async () => {
    mockEventsFetch();
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/user\.login_succeeded/)).toBeInTheDocument();
    });
  });
});
