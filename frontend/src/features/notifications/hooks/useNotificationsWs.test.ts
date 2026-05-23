import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { toast } from 'sonner';
import type { MockInstance } from 'vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { notificationKeys } from '../api/notifications';

import { useNotificationsWs } from './useNotificationsWs';

const mockUser = {
  value: {
    id: '88888888-8888-4888-8888-888888888888',
    username: 'alice',
    role: 'order_manager',
  } as { id: string; username: string; role: string } | null,
};

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => mockUser.value,
}));

vi.mock('sonner', () => ({
  toast: {
    info: vi.fn(),
  },
}));

type Listener<E> = ((event: E) => void) | null;

class MockWebSocket {
  static instances: MockWebSocket[] = [];

  onopen: Listener<Event> = null;

  onmessage: Listener<MessageEvent> = null;

  onclose: Listener<CloseEvent> = null;

  onerror: Listener<Event> = null;

  close = vi.fn();

  constructor(public readonly url: string) {
    MockWebSocket.instances.push(this);
  }

  fireOpen(): void {
    this.onopen?.(new Event('open'));
  }

  fireMessage(raw: string | object): void {
    const data = typeof raw === 'string' ? raw : JSON.stringify(raw);
    this.onmessage?.({ data } as MessageEvent);
  }

  fireClose(code = 1006): void {
    this.onclose?.({ code, wasClean: code === 1000 } as CloseEvent);
  }
}

function latestSocket(): MockWebSocket {
  const socket = MockWebSocket.instances.at(-1);
  if (!socket) throw new Error('No WebSocket has been constructed.');
  return socket;
}

let queryClient: QueryClient;
let invalidateSpy: MockInstance;

function renderNotificationsWs() {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries') as unknown as MockInstance;

  function Wrapper({ children }: { children: ReactNode }): JSX.Element {
    return createElement(QueryClientProvider, { client: queryClient }, children);
  }

  return renderHook(
    () => {
      useNotificationsWs();
    },
    { wrapper: Wrapper },
  );
}

beforeEach(() => {
  mockUser.value = {
    id: '88888888-8888-4888-8888-888888888888',
    username: 'alice',
    role: 'order_manager',
  };
  MockWebSocket.instances = [];
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', MockWebSocket);
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { protocol: 'http:', host: 'localhost:5173' } as Location,
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('useNotificationsWs', () => {
  it('does not open a socket without an authenticated user', () => {
    mockUser.value = null;

    renderNotificationsWs();

    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('invalidates notifications and shows a toast for notification.created', () => {
    renderNotificationsWs();

    latestSocket().fireMessage({
      type: 'notification.created',
      data: {
        id: '11111111-1111-4111-8111-111111111111',
        type: 'order_status_changed',
        message: 'Order date changed',
        is_read: false,
        created_at: '2026-05-21T00:00:00.000Z',
      },
    });

    // Invalidate fires immediately so the badge updates without waiting
    // for the coalesce window, but the toast is debounced.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: notificationKeys.all });
    expect(toast.info).not.toHaveBeenCalled();

    vi.advanceTimersByTime(500);
    expect(toast.info).toHaveBeenCalledWith(
      '新通知',
      expect.objectContaining({ description: 'Order date changed' }),
    );
  });

  it('coalesces a burst of notification.created into a single toast', () => {
    renderNotificationsWs();

    const socket = latestSocket();
    for (let i = 0; i < 3; i += 1) {
      socket.fireMessage({
        type: 'notification.created',
        data: {
          id: `11111111-1111-4111-8111-11111111111${i}`,
          type: 'order_status_changed',
          message: `msg ${i}`,
          is_read: false,
          created_at: '2026-05-21T00:00:00.000Z',
        },
      });
    }

    expect(toast.info).not.toHaveBeenCalled();
    vi.advanceTimersByTime(500);

    expect(toast.info).toHaveBeenCalledTimes(1);
    expect(toast.info).toHaveBeenCalledWith(
      '3 則新通知',
      expect.objectContaining({ description: '通知中心已更新' }),
    );
  });

  it('reconnects after an abnormal close', () => {
    renderNotificationsWs();

    latestSocket().fireClose(1006);

    expect(MockWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1_000);
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('does not reconnect after auth failure', () => {
    renderNotificationsWs();

    latestSocket().fireClose(4401);
    vi.advanceTimersByTime(60_000);

    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('invalidates notifications after a reconnect opens', () => {
    renderNotificationsWs();
    latestSocket().fireOpen();
    invalidateSpy.mockClear();

    latestSocket().fireClose(1006);
    vi.advanceTimersByTime(1_000);
    latestSocket().fireOpen();

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: notificationKeys.all });
  });

  it('cleans up a pending reconnect on unmount', () => {
    const view = renderNotificationsWs();
    latestSocket().fireClose(1006);

    view.unmount();
    vi.advanceTimersByTime(60_000);

    expect(MockWebSocket.instances).toHaveLength(1);
  });
});
