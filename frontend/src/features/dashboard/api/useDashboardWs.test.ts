/**
 * useDashboardWs — RED-first tests for the dashboard WebSocket hook.
 *
 * The hook opens a connection to ``/api/v1/ws``, listens for backend
 * scheduling events, and translates each event into a ``queryClient
 * .invalidateQueries`` call so the existing dashboard query hooks
 * re-fetch on demand instead of waiting for the next polling tick.
 *
 * These tests drive the hook's full surface:
 *   - connection lifecycle (open / close / reconnect)
 *   - event → query-key mapping (one test per event type)
 *   - malformed input tolerance (bad JSON / missing type / unknown type)
 *   - exponential backoff (doubling + 30s cap + reset on success)
 *   - cleanup on unmount (cancel pending reconnect, close socket)
 *
 * Real ``WebSocket`` is replaced by ``MockWebSocket`` via ``vi.stubGlobal``;
 * each instance is captured so tests can drive ``onopen / onmessage /
 * onclose`` synchronously. Fake timers cover the backoff arithmetic
 * without sleeping.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, renderHook } from '@testing-library/react';
import * as React from 'react';
import type { MockInstance } from 'vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useDashboardWs } from './useDashboardWs';

// ---------------------------------------------------------------------------
// Auth mock
// ---------------------------------------------------------------------------

const mockUser = {
  value: { id: 'u', username: 'alice', role: 'order_manager' } as {
    id: string;
    username: string;
    role: string;
  } | null,
};

vi.mock('@/lib/auth', () => ({
  useCurrentUser: () => mockUser.value,
  useCurrentRole: () => mockUser.value?.role ?? null,
}));

// ---------------------------------------------------------------------------
// MockWebSocket — captures instances + exposes synchronous fire helpers
// ---------------------------------------------------------------------------

type Listener<E> = ((e: E) => void) | null;

class MockWebSocket {
  static instances: MockWebSocket[] = [];

  static readonly CONNECTING = 0;

  static readonly OPEN = 1;

  static readonly CLOSING = 2;

  static readonly CLOSED = 3;

  readyState: number = MockWebSocket.CONNECTING;

  onopen: Listener<Event> = null;

  onmessage: Listener<MessageEvent> = null;

  onclose: Listener<CloseEvent> = null;

  onerror: Listener<Event> = null;

  close = vi.fn((code?: number): void => {
    this.readyState = MockWebSocket.CLOSED;
    // The browser would fire onclose with whatever code was provided. We
    // don't fire here — tests that want a server-side close call
    // ``fireClose`` explicitly with the code under test.
    void code;
  });

  constructor(public readonly url: string) {
    MockWebSocket.instances.push(this);
  }

  fireOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event('open'));
  }

  fireMessage(raw: string | object): void {
    const data = typeof raw === 'string' ? raw : JSON.stringify(raw);
    this.onmessage?.({ data } as MessageEvent);
  }

  fireClose(code = 1006, reason = ''): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code, reason, wasClean: code === 1000 } as CloseEvent);
  }
}

function latestSocket(): MockWebSocket {
  const last = MockWebSocket.instances.at(-1);
  if (!last) throw new Error('No WebSocket has been constructed yet.');
  return last;
}

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------

let qc: QueryClient;
// Typed loosely because vitest's `vi.spyOn` return type is awkward to
// satisfy on QueryClient's overloaded `invalidateQueries`. We only
// touch `.mock.calls` in the helper below.
let invalidateSpy: MockInstance;

function renderWs() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  invalidateSpy = vi.spyOn(qc, 'invalidateQueries') as unknown as MockInstance;
  function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  }
  return renderHook(
    () => {
      useDashboardWs();
    },
    { wrapper: Wrapper },
  );
}

function expectInvalidatedKeys(keys: readonly (readonly unknown[])[]): void {
  const actual = invalidateSpy.mock.calls.map((c): unknown => {
    const arg = c[0] as { queryKey?: unknown } | undefined;
    return arg?.queryKey;
  });
  expect(actual).toEqual(expect.arrayContaining([...keys]));
  expect(actual).toHaveLength(keys.length);
}

beforeEach(() => {
  mockUser.value = { id: 'u', username: 'alice', role: 'order_manager' };
  MockWebSocket.instances = [];
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useDashboardWs — connection lifecycle', () => {
  it('does not open a connection when user is null', () => {
    mockUser.value = null;
    renderWs();
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('opens a connection to /api/v1/ws when user is authenticated', () => {
    renderWs();
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(latestSocket().url).toBe('ws://localhost:5173/api/v1/ws');
  });

  it('uses wss:// when page is served over https://', () => {
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { protocol: 'https:', host: 'app.example.com' } as Location,
    });
    renderWs();
    expect(latestSocket().url).toBe('wss://app.example.com/api/v1/ws');
  });
});

describe('useDashboardWs — event → query-key mapping', () => {
  it('schedule.updated invalidates capacity + pending-ops + orders + status', () => {
    renderWs();
    latestSocket().fireMessage({ type: 'schedule.updated' });
    expectInvalidatedKeys([
      ['schedule', 'capacity'],
      ['schedule', 'pending-ops'],
      ['orders', 'snapshot'],
      ['schedule', 'status'],
    ]);
  });

  it('schedule.materialized invalidates capacity + orders snapshot only', () => {
    renderWs();
    latestSocket().fireMessage({ type: 'schedule.materialized' });
    expectInvalidatedKeys([
      ['schedule', 'capacity'],
      ['orders', 'snapshot'],
    ]);
  });

  it('schedule.compound_accepted invalidates pending-ops + status', () => {
    renderWs();
    latestSocket().fireMessage({ type: 'schedule.compound_accepted', compound_id: 'c' });
    expectInvalidatedKeys([
      ['schedule', 'pending-ops'],
      ['schedule', 'status'],
    ]);
  });

  it('schedule.compound_failed invalidates pending-ops + status', () => {
    renderWs();
    latestSocket().fireMessage({ type: 'schedule.compound_failed', compound_id: 'c' });
    expectInvalidatedKeys([
      ['schedule', 'pending-ops'],
      ['schedule', 'status'],
    ]);
  });

  it('schedule.compound_cancelled invalidates pending-ops only', () => {
    renderWs();
    latestSocket().fireMessage({ type: 'schedule.compound_cancelled', compound_id: 'c' });
    expectInvalidatedKeys([['schedule', 'pending-ops']]);
  });

  it('schedule.rebuild_skipped invalidates pending-ops + orders snapshot', () => {
    renderWs();
    latestSocket().fireMessage({ type: 'schedule.rebuild_skipped' });
    expectInvalidatedKeys([
      ['schedule', 'pending-ops'],
      ['orders', 'snapshot'],
    ]);
  });
});

describe('useDashboardWs — bad input tolerance', () => {
  it('ignores unknown event types (no invalidation, no throw)', () => {
    renderWs();
    expect(() => {
      latestSocket().fireMessage({ type: 'schedule.never_emitted_by_backend' });
    }).not.toThrow();
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it('ignores messages without a string type field', () => {
    renderWs();
    latestSocket().fireMessage({ type: 42 } as unknown as object);
    latestSocket().fireMessage({});
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it('ignores malformed JSON (no throw)', () => {
    renderWs();
    expect(() => {
      latestSocket().fireMessage('not-json{');
    }).not.toThrow();
    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});

describe('useDashboardWs — reconnect behaviour', () => {
  it('reconnects 1s after a non-auth close (1006)', () => {
    vi.useFakeTimers();
    renderWs();
    latestSocket().fireClose(1006);

    expect(MockWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('doubles backoff on consecutive failures, capped at 30s', () => {
    vi.useFakeTimers();
    renderWs();

    // Drop each new connection right after it opens; the backoff must
    // double each cycle until it caps at 30s.
    const expectedDelays = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000];
    expectedDelays.forEach((delay) => {
      latestSocket().fireClose(1006);
      vi.advanceTimersByTime(delay - 1);
      // Reconnect hasn't fired yet (one tick away).
      const before = MockWebSocket.instances.length;
      vi.advanceTimersByTime(1);
      expect(MockWebSocket.instances.length).toBe(before + 1);
    });
  });

  it('resets backoff to 1s after a successful open', () => {
    vi.useFakeTimers();
    renderWs();
    // Force backoff to grow once.
    latestSocket().fireClose(1006);
    vi.advanceTimersByTime(1_000); // reconnect #1
    latestSocket().fireClose(1006);
    vi.advanceTimersByTime(2_000); // reconnect #2

    // Successful open resets backoff.
    latestSocket().fireOpen();
    latestSocket().fireClose(1006);
    // After reset, next reconnect should fire at 1s, not 4s.
    vi.advanceTimersByTime(999);
    const before = MockWebSocket.instances.length;
    vi.advanceTimersByTime(1);
    expect(MockWebSocket.instances.length).toBe(before + 1);
  });

  it('does NOT reconnect after a 4401 auth-failed close', () => {
    vi.useFakeTimers();
    renderWs();
    latestSocket().fireClose(4401);
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('does NOT reconnect after a 1000 normal close', () => {
    vi.useFakeTimers();
    renderWs();
    latestSocket().fireClose(1000);
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
  });
});

describe('useDashboardWs — cleanup', () => {
  it('closes the socket on unmount', () => {
    const view = renderWs();
    const ws = latestSocket();
    view.unmount();
    expect(ws.close).toHaveBeenCalledTimes(1);
  });

  it('cancels pending reconnect timer on unmount', () => {
    vi.useFakeTimers();
    const view = renderWs();
    latestSocket().fireClose(1006);
    // A reconnect is scheduled. Unmount BEFORE the timer fires.
    view.unmount();
    vi.advanceTimersByTime(60_000);
    // No new socket constructed after unmount.
    expect(MockWebSocket.instances).toHaveLength(1);
  });
});
