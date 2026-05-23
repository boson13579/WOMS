import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type * as React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { toast } from 'sonner';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NotificationsPage } from './NotificationsPage';

const mockUser = {
  id: '88888888-8888-8888-8888-888888888888',
  username: 'alice',
  role: 'order_manager',
};

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => mockUser,
  useCurrentRole: () => mockUser.role,
  useCurrentUserId: () => mockUser.id,
}));

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
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

const mockUnreadNotifications = {
  items: [
    {
      id: 'abcdef11-2222-3333-4444-555555555555',
      user_id: mockUser.id,
      order_id: '11111111-1111-1111-1111-111111111111',
      type: 'order_locked',
      message: '訂單 ORD-001 已被鎖定處理中',
      is_read: false,
      created_at: new Date(Date.now() - 5 * 60000).toISOString(), // 5 mins ago
    },
    {
      id: 'abcdef22-2222-3333-4444-555555555555',
      user_id: mockUser.id,
      order_id: '22222222-2222-2222-2222-222222222222',
      type: 'order_status_changed',
      message: '訂單 ORD-002 狀態已變更為 scheduled',
      is_read: false,
      created_at: new Date(Date.now() - 60 * 60000).toISOString(), // 1 hour ago
    },
  ],
  total: 2,
};

const mockAllNotifications = {
  items: [
    ...mockUnreadNotifications.items,
    {
      id: 'abcdef33-2222-3333-4444-555555555555',
      user_id: mockUser.id,
      order_id: '33333333-3333-3333-3333-333333333333',
      type: 'order_cancelled',
      message: '訂單 ORD-003 已被取消',
      is_read: true,
      created_at: new Date(Date.now() - 24 * 3600000).toISOString(), // 24 hours ago
    },
  ],
  total: 3,
};

function setupFetchMock(unreadList = mockUnreadNotifications, allList = mockAllNotifications) {
  vi.mocked(global.fetch).mockImplementation((url) => {
    const u = new URL(String(url), 'http://localhost');
    if (u.pathname === '/api/v1/notifications') {
      const isAll = u.searchParams.get('all') === 'true';
      return Promise.resolve(
        new Response(JSON.stringify(isAll ? allList : unreadList), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    if (u.pathname.endsWith('/read')) {
      const segments = u.pathname.split('/');
      const id = segments[segments.length - 2];
      const match = allList.items.find((i) => i.id === id);
      return Promise.resolve(
        new Response(JSON.stringify({ ...(match ?? {}), is_read: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    if (u.pathname === '/api/v1/notifications/read-all') {
      return Promise.resolve(
        new Response(JSON.stringify({ updated: unreadList.total }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    return Promise.resolve(new Response('Not Found', { status: 404 }));
  });
}

describe('NotificationsPage', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    setupFetchMock();
  });

  it('renders unread notifications list by default', async () => {
    render(<NotificationsPage />, { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(screen.getByText('訂單 ORD-001 已被鎖定處理中')).toBeInTheDocument();
    });

    expect(screen.getByText('訂單 ORD-002 狀態已變更為 scheduled')).toBeInTheDocument();
    expect(screen.queryByText('訂單 ORD-003 已被取消')).not.toBeInTheDocument();
    expect(screen.getByText('未讀通知')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument(); // Badge count
  });

  it('shows a fallback label for invalid timestamps', async () => {
    setupFetchMock({
      items: [
        {
          ...mockUnreadNotifications.items[0],
          message: 'Broken time notification',
          created_at: 'not-a-date',
        },
      ],
      total: 1,
    });

    render(<NotificationsPage />, { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(screen.getByText('Broken time notification')).toBeInTheDocument();
    });
    expect(screen.getByText('未知時間')).toBeInTheDocument();
  });

  it('switches between Unread and All tabs and displays correct counts', async () => {
    const user = userEvent.setup();
    render(<NotificationsPage />, { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(screen.getByText('訂單 ORD-001 已被鎖定處理中')).toBeInTheDocument();
    });

    // Tap All tab
    const allTab = screen.getByRole('button', { name: /全部通知/i });
    await user.click(allTab);

    await waitFor(() => {
      expect(screen.getByText('訂單 ORD-003 已被取消')).toBeInTheDocument();
    });

    expect(screen.getByText('訂單 ORD-001 已被鎖定處理中')).toBeInTheDocument();
    expect(screen.getByText('訂單 ORD-002 狀態已變更為 scheduled')).toBeInTheDocument();
  });

  it('calls PATCH /read-all when clicking Mark All as Read', async () => {
    const user = userEvent.setup();
    render(<NotificationsPage />, { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /全部標記為已讀/i })).toBeInTheDocument();
    });

    const markAllBtn = screen.getByRole('button', { name: /全部標記為已讀/i });
    await user.click(markAllBtn);

    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith(
        '已將所有通知標記為已讀',
        expect.objectContaining({
          description: '成功更新了 2 筆通知。',
        }),
      );
    });
  });

  it('marks a single notification as read', async () => {
    const user = userEvent.setup();
    render(<NotificationsPage />, { wrapper: makeWrapper() });

    await waitFor(() => {
      expect(screen.getByText('訂單 ORD-001 已被鎖定處理中')).toBeInTheDocument();
    });

    const markSingleButtons = screen.getAllByRole('button', { name: '標記為已讀' });
    expect(markSingleButtons).toHaveLength(2);

    await user.click(markSingleButtons[0]);

    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith('已標記為已讀');
    });
  });
});
